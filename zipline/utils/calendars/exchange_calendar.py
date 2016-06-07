#
# Copyright 2016 Quantopian, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from abc import (
    ABCMeta,
    abstractproperty,
    abstractmethod,
)

import pandas as pd
import numpy as np
from pandas import (
    DataFrame,
    date_range,
    DateOffset,
    DatetimeIndex,
)
from pandas.tseries.offsets import CustomBusinessDay
from six import with_metaclass

from zipline.errors import (
    InvalidCalendarName,
    CalendarNameCollision,
)
from zipline.utils.memoize import remember_last

from .calendar_helpers import (
    next_scheduled_day,
    previous_scheduled_day,
    next_open_and_close,
    previous_open_and_close,
    scheduled_day_distance,
    minutes_for_day,
    days_in_range,
    minutes_for_days_in_range,
    add_scheduled_days,
    next_scheduled_minute,
    previous_scheduled_minute,
)

start_default = pd.Timestamp('1990-01-01', tz='UTC')
end_base = pd.Timestamp('today', tz='UTC')
# Give an aggressive buffer for logic that needs to use the next trading
# day or minute.
end_default = end_base + pd.Timedelta(days=365)

NANOS_IN_MINUTE = 60000000000


def days_at_time(days, t, tz, day_offset=0):
    """
    Shift an index of days to time t, interpreted in tz.

    Overwrites any existing tz info on the input.

    Parameters
    ----------
    days : DatetimeIndex
        The "base" time which we want to change.
    t : datetime.time
        The time we want to offset @days by
    tz : pytz.timezone
        The timezone which these times represent
    day_offset : int
        The number of days we want to offset @days by
    """
    days = DatetimeIndex(days).tz_localize(None).tz_localize(tz)
    days_offset = days + DateOffset(day_offset)
    shifted_days = DatetimeIndex(days_offset.shift(
        1, freq=DateOffset(hour=t.hour, minute=t.minute, second=t.second)
    ))
    return shifted_days.tz_localize(None).tz_localize(tz).tz_convert('UTC')


def holidays_at_time(calendar, start, end, time, tz):
    return days_at_time(
        calendar.holidays(
            # Workaround for https://github.com/pydata/pandas/issues/9825.
            start.tz_localize(None),
            end.tz_localize(None),
        ),
        time,
        tz=tz,
    )


def _overwrite_special_dates(midnight_utcs,
                             opens_or_closes,
                             special_opens_or_closes):
    """
    Overwrite dates in open_or_closes with corresponding dates in
    special_opens_or_closes, using midnight_utcs for alignment.
    """
    # Short circuit when nothing to apply.
    if not len(special_opens_or_closes):
        return

    len_m, len_oc = len(midnight_utcs), len(opens_or_closes)
    if len_m != len_oc:
        raise ValueError(
            "Found misaligned dates while building calendar.\n"
            "Expected midnight_utcs to be the same length as open_or_closes,\n"
            "but len(midnight_utcs)=%d, len(open_or_closes)=%d" % len_m, len_oc
        )

    # Find the array indices corresponding to each special date.
    indexer = midnight_utcs.get_indexer(special_opens_or_closes.normalize())

    # -1 indicates that no corresponding entry was found.  If any -1s are
    # present, then we have special dates that doesn't correspond to any
    # trading day.
    if -1 in indexer:
        bad_dates = list(special_opens_or_closes[indexer == -1])
        raise ValueError("Special dates %s are not trading days." % bad_dates)

    # NOTE: This is a slightly dirty hack.  We're in-place overwriting the
    # internal data of an Index, which is conceptually immutable.  Since we're
    # maintaining sorting, this should be ok, but this is a good place to
    # sanity check if things start going haywire with calendar computations.
    opens_or_closes.values[indexer] = special_opens_or_closes.values


class ExchangeCalendar(with_metaclass(ABCMeta)):
    """
    An ExchangeCalendar represents the timing information of a single market
    exchange.

    Properties
    ----------
    name : str
        The name of this exchange calendar.
        e.g.: 'NYSE', 'LSE', 'CME Energy'
    tz : timezone
        The native timezone of the exchange.
    """

    def __init__(self, start=start_default, end=end_default):
        tz = self.tz
        open_offset = self.open_offset
        close_offset = self.close_offset

        # Define those days on which the exchange is usually open.
        self.day = CustomBusinessDay(
            holidays=self.holidays_adhoc,
            calendar=self.holidays_calendar,
        )

        # Midnight in UTC for each trading day.
        _all_days = date_range(start, end, freq=self.day, tz='UTC')

        # `DatetimeIndex`s of standard opens/closes for each day.
        self._opens = days_at_time(_all_days, self.open_time, tz, open_offset)
        self._closes = days_at_time(
            _all_days, self.close_time, tz, close_offset
        )

        # `DatetimeIndex`s of nonstandard opens/closes
        _special_opens = self._special_opens(start, end)
        _special_closes = self._special_closes(start, end)

        # Overwrite the special opens and closes on top of the standard ones.
        _overwrite_special_dates(_all_days, self._opens, _special_opens)
        _overwrite_special_dates(_all_days, self._closes, _special_closes)

        # In pandas 0.16.1 _opens and _closes will lose their timezone
        # information. This looks like it has been resolved in 0.17.1.
        # http://pandas.pydata.org/pandas-docs/stable/whatsnew.html#datetime-with-tz  # noqa
        self.schedule = DataFrame(
            index=_all_days,
            columns=['market_open', 'market_close'],
            data={
                'market_open': self._opens,
                'market_close': self._closes,
            },
            dtype='datetime64[ns]',
        )

        self.first_trading_day = _all_days[0]
        self.last_trading_day = _all_days[-1]
        self.early_closes = DatetimeIndex(
            _special_closes.map(self.session_date)
        )

    def next_trading_day(self, date):
        return next_scheduled_day(
            date,
            last_trading_day=self.last_trading_day,
            is_scheduled_day_hook=self.is_open_on_day,
        )

    def previous_trading_day(self, date):
        return previous_scheduled_day(
            date,
            first_trading_day=self.first_trading_day,
            is_scheduled_day_hook=self.is_open_on_day,
        )

    def next_open_and_close(self, date):
        return next_open_and_close(
            date,
            open_and_close_hook=self.open_and_close,
            next_scheduled_day_hook=self.next_trading_day,
        )

    def previous_open_and_close(self, date):
        return previous_open_and_close(
            date,
            open_and_close_hook=self.open_and_close,
            previous_scheduled_day_hook=self.previous_trading_day,
        )

    def trading_day_distance(self, first_date, second_date):
        return scheduled_day_distance(
            first_date, second_date,
            all_days=self.all_trading_days,
        )

    def trading_minutes_for_day(self, day):
        return minutes_for_day(
            day,
            open_and_close_hook=self.open_and_close,
        )

    def trading_days_in_range(self, start, end):
        return days_in_range(
            start, end,
            all_days=self.all_trading_days,
        )

    def trading_minutes_for_days_in_range(self, start, end):
        return minutes_for_days_in_range(
            start, end,
            days_in_range_hook=self.trading_days_in_range,
            minutes_for_day_hook=self.trading_minutes_for_day,
        )

    def add_trading_days(self, n, date):
        """
        Adds n trading days to date. If this would fall outside of the
        ExchangeCalendar, a NoFurtherDataError is raised.

        Parameters
        ----------
        n : int
            The number of days to add to date, this can be positive or
            negative.
        date : datetime
            The date to add to.

        Returns
        -------
        datetime
            n trading days added to date.
        """
        return add_scheduled_days(
            n, date,
            next_scheduled_day_hook=self.next_trading_day,
            previous_scheduled_day_hook=self.previous_trading_day,
            all_trading_days=self.all_trading_days,
        )

    def next_trading_minute(self, start):
        return next_scheduled_minute(
            start,
            is_scheduled_day_hook=self.is_open_on_day,
            open_and_close_hook=self.open_and_close,
            next_open_and_close_hook=self.next_open_and_close,
        )

    def previous_trading_minute(self, start):
        return previous_scheduled_minute(
            start,
            is_scheduled_day_hook=self.is_open_on_day,
            open_and_close_hook=self.open_and_close,
            previous_open_and_close_hook=self.previous_open_and_close,
        )

    def _special_dates(self, calendars, ad_hoc_dates, start_date, end_date):
        """
        Union an iterable of pairs of the form

        (time, calendar)

        and an iterable of pairs of the form

        (time, [dates])

        (This is shared logic for computing special opens and special closes.)
        """
        tz = self.native_timezone
        _dates = DatetimeIndex([], tz='UTC').union_many(
            [
                holidays_at_time(calendar, start_date, end_date, time_, tz)
                for time_, calendar in calendars
            ] + [
                days_at_time(datetimes, time_, tz)
                for time_, datetimes in ad_hoc_dates
            ]
        )
        return _dates[(_dates >= start_date) & (_dates <= end_date)]

    def _special_opens(self, start, end):
        return self._special_dates(
            self.special_opens_calendars,
            self.special_opens_adhoc,
            start,
            end,
        )

    def _special_closes(self, start, end):
        return self._special_dates(
            self.special_closes_calendars,
            self.special_closes_adhoc,
            start,
            end,
        )

    @abstractproperty
    def name(self):
        """
        The name of this exchange calendar.
        E.g.: 'NYSE', 'LSE', 'CME Energy'
        """
        raise NotImplementedError()

    @abstractproperty
    def tz(self):
        """
        The native timezone of the exchange.

        SD: Not clear that this needs to be exposed.
        """
        raise NotImplementedError()

    @abstractmethod
    def is_open_on_minute(self, dt):
        """
        Is the exchange open at minute @dt.

        Parameters
        ----------
        dt : Timestamp

        Returns
        -------
        bool
            True if  exchange is open at the given dt, otherwise False.
        """
        raise NotImplementedError()

    @abstractmethod
    def is_open_on_day(self, dt):
        """
        Is the exchange open anytime during @dt.

        SD: Need to decide whether this method answers the question:
        - Is exchange open at any time during the calendar day containing dt
        or
        - Is exchange open at any time during the trading session containg dt.
        Semantically it seems that the first makes more sense.

        Parameters
        ----------
        dt : Timestamp
            The UTC-canonicalized date.

        Returns
        -------
        bool
            True if exchange is open at any time during @dt.
        """
        raise NotImplementedError()

    @abstractmethod
    def trading_days(self, start, end):
        """
        Calculates all of the exchange sessions between the given
        start and end.

        SD: Presumably @start and @end are UTC-canonicalized, as our exchange
        sessions are. If not, then it's not clear how this method should behave
        if @start and @end are both in the middle of the day.

        Parameters
        ----------
        start : Timestamp
        end : Timestamp

        Returns
        -------
        DatetimeIndex
            A DatetimeIndex populated with all of the trading days between
            the given start and end.
        """
        raise NotImplementedError()

    @property
    def all_trading_days(self):
        return self.schedule.index

    @property
    @remember_last
    def all_trading_minutes(self):
        opens_in_ns = \
            self._opens.values.astype('datetime64[ns]').astype(np.int64)

        closes_in_ns = \
            self._closes.values.astype('datetime64[ns]').astype(np.int64)

        deltas = closes_in_ns - opens_in_ns

        # + 1 because we want 390 days per standard day, not 389
        daily_sizes = (deltas / NANOS_IN_MINUTE) + 1
        num_minutes = np.sum(daily_sizes).astype(np.int64)

        # One allocation for the entire thing. This assumes that each day
        # represents a contiguous block of minutes, which might not always
        # be the case in the future.
        all_minutes = np.empty(num_minutes, dtype='datetime64[ns]')

        idx = 0
        for day_idx, size in enumerate(daily_sizes):
            # lots of small allocations, but it's fast enough for now.
            all_minutes[idx:(idx + size)] = \
                np.arange(
                    opens_in_ns[day_idx],
                    closes_in_ns[day_idx] + NANOS_IN_MINUTE,
                    NANOS_IN_MINUTE
                )

            idx += size

        return DatetimeIndex(all_minutes).tz_localize("UTC")

    @abstractmethod
    def open_and_close(self, date):
        """
        Given a UTC-canonicalized date, returns a tuple of timestamps of the
        open and close of the exchange session on that date.

        SD: Can @date be an arbitrary datetime, or should we first map it to
        and exchange session using session_date. Need to check what the
        consumers expect.

        Parameters
        ----------
        date : Timestamp
            The UTC-canonicalized date whose open and close are needed.

        Returns
        -------
        (Timestamp, Timestamp)
            The open and close for the given date.
        """
        raise NotImplementedError()

    @abstractmethod
    def session_date(self, dt):
        """
        Given a time, returns the UTC-canonicalized date of the exchange
        session in which the time belongs. If the time is not in an exchange
        session (while the market is closed), returns the date of the next
        exchange session after the time.

        Parameters
        ----------
        dt : Timestamp

        Returns
        -------
        Timestamp
            The date of the exchange session in which dt belongs.
        """
        raise NotImplementedError()


_static_calendars = {}


def get_calendar(name, start=None, end=None):
    """
    Retrieves an instance of an ExchangeCalendar whose name is given.

    Parameters
    ----------
    name : str
        The name of the ExchangeCalendar to be retrieved.
    """
    # First, check if the calendar is already registered
    if name not in _static_calendars:

        kwargs = {}
        if start is not None:
            kwargs['start'] = start
        if end is not None:
            kwargs['end'] = end

        # Check if it is a lazy calendar. If so, build and register it.
        if name == 'NYSE':
            from zipline.utils.calendars.exchange_calendar_nyse \
                import NYSEExchangeCalendar
            nyse_cal = NYSEExchangeCalendar(**kwargs)
            register_calendar(nyse_cal)

        elif name == 'CME':
            from zipline.utils.calendars.exchange_calendar_cme \
                import CMEExchangeCalendar
            cme_cal = CMEExchangeCalendar(**kwargs)
            register_calendar(cme_cal)

        elif name == 'BMF':
            from zipline.utils.calendars.exchange_calendar_bmf \
                import BMFExchangeCalendar
            bmf_cal = BMFExchangeCalendar(**kwargs)
            register_calendar(bmf_cal)

        elif name == 'LSE':
            from zipline.utils.calendars.exchange_calendar_lse \
                import LSEExchangeCalendar
            lse_cal = LSEExchangeCalendar(**kwargs)
            register_calendar(lse_cal)

        elif name == 'TSX':
            from zipline.utils.calendars.exchange_calendar_tsx \
                import TSXExchangeCalendar
            tsx_cal = TSXExchangeCalendar(**kwargs)
            register_calendar(tsx_cal)

        else:
            # It's not a lazy calendar, so raise an exception
            raise InvalidCalendarName(calendar_name=name)

    return _static_calendars[name]


def deregister_calendar(cal_name):
    """
    If a calendar is registered with the given name, it is de-registered.

    Parameters
    ----------
    cal_name : str
        The name of the calendar to be deregistered.
    """
    try:
        _static_calendars.pop(cal_name)
    except KeyError:
        pass


def clear_calendars():
    """
    Deregisters all current registered calendars
    """
    _static_calendars.clear()


def register_calendar(calendar, force=False):
    """
    Registers a calendar for retrieval by the get_calendar method.

    Parameters
    ----------
    calendar : ExchangeCalendar
        The calendar to be registered for retrieval.
    force : bool, optional
        If True, old calendars will be overwritten on a name collision.
        If False, name collisions will raise an exception. Default: False.

    Raises
    ------
    CalendarNameCollision
        If a calendar is already registered with the given calendar's name.
    """
    # If we are forcing the registration, remove an existing calendar with the
    # same name.
    if force:
        deregister_calendar(calendar.name)

    # Check if we are already holding a calendar with the same name
    if calendar.name in _static_calendars:
        raise CalendarNameCollision(calendar_name=calendar.name)

    _static_calendars[calendar.name] = calendar
