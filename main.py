"""
monitor connectivity to a landmark
reports outages, ignoring the ones due to a down interface
"""

# pylint: disable=expression-not-assigned

import typing
from enum import Enum
import signal
from datetime import datetime as DateTime
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
import statistics
import asyncio

import aioping


class Context:
    """
    the context for all the pings
    """

    def __init__(self, landmark, iface, timeout):
        self.landmark = landmark
        self.iface = iface
        self.timeout = timeout

    async def is_connected(self) -> bool:
        """
        check if the interface is currently active
        """
        ifconfig = await asyncio.create_subprocess_shell(
            f"ifconfig {self.iface}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        stdout, _ = await ifconfig.communicate()
        if ifconfig.returncode != 0:
            return False
        if not stdout:
            return False
        if stdout.decode().find('status: active') < 0:
            return False
        return True

    async def low_level_ping(self) -> float | bool:
        """
        lowest level ping
        """
        try:
            delay = await aioping.ping(self.landmark,
                                       timeout=self.timeout) * 1000
            return delay

        except TimeoutError:
            return False

    async def _ping_once(self, verbose) -> float | bool:
        """
        True means the interface is down - it is deemed OK in that case
        False means it's up but the landmark is not reachable
        float is the delay in ms, it means success
        """
        if not await self.is_connected():
            verbose and print(f'{self.iface} is not online')
            return True
        delay = await self.low_level_ping()
        if delay is False:
            verbose and print(f'{self.landmark} is not reachable')
            return False
        verbose and print(
            f'{self.iface=} is online and icmp returned in {delay:.2f} ms')
        return delay

    class Result:
        """
        at what time does it start, and what's the result
        """
        def __init__(self):
            self.time = DateTime.now()
            self.ping_result = None

        def __repr__(self):
            return f'<Result {self.time:%H:%M:%S} {self.ping_result}>'

        def capture(self, ping_result) -> "Result":
            """
            captures this result
            """
            self.ping_result = ping_result
            return self

    async def ping_once(self, verbose) -> Result:
        """
        run one ping, and capture starting time and outcome
        in a Result instance
        """
        # capture time at beginning of ping_once
        result = Context.Result()
        ping_result = await self._ping_once(verbose)
        return result.capture(ping_result)


class Stats:
    """
    the list of delays during a live period, for stats
    """
    def __init__(self):
        self.delays = []

    def record(self, delay):
        """
        record a delay
        """
        if isinstance(delay, float):
            self.delays.append(delay)

    def report_line(self):
        """
        one-line report with 5 items
        number mean stdev min max
        """
        if not self.delays:
            return '0 0.00 0.00 0.00'
        # cannot compute stdev with a single point
        if len(self.delays) == 1:
            single = self.delays[0]
            return f'1 {single:.2f} 0.00 {single:.2f} {single:.2f}'
        return (
            f'{len(self.delays)}'
            f' {statistics.mean(self.delays):.2f}'
            f' {statistics.stdev(self.delays):.2f}'
            f' {min(self.delays):.2f}'
            f' {max(self.delays):.2f}'
        )


class StateMachine:
    """
    the state machine that keeps track of the current state
    """
    class State(Enum):
        """
        the possible states
        """
        UNKNOWN = 0
        ONLINE = 1
        OFFLINE = 2

    def __init__(self, logfile: typing.IO):
        self.logfile = logfile
        self._state = self.State.UNKNOWN
        self._state_time = None
        self.stats = None

    def handle_result(self, result: Context.Result):
        """
        handle a result, and change state accordingly
        """
        match self._state:
            case self.State.UNKNOWN:
                if result.ping_result is False:
                    self._state = self.State.OFFLINE
                    self._state_time = result.time
                else:
                    self._state = self.State.ONLINE
                    self._state_time = result.time
                    self.stats = Stats()
                    if isinstance(result.ping_result, float):
                        self.stats.record(result.ping_result)
            case self.State.ONLINE:
                if result.ping_result is False:
                    # OUTAGE STARTS
                    self.report_live()
                    self._state = self.State.OFFLINE
                    self._state_time = result.time
                    self.stats = None
                else:
                    if isinstance(result.ping_result, float):
                        self.stats.record(result.ping_result)
            case self.State.OFFLINE:
                if result.ping_result:
                    # OUTAGE ENDS
                    self.report_outage()
                    self._state = self.State.ONLINE
                    self._state_time = result.time
                    self.stats = Stats()
                    if isinstance(result.ping_result, float):
                        self.stats.record(result.ping_result)
                else:
                    # outage continues
                    pass

    def cleanup(self):
        """
        let's not miss it if the program gets
        interrupted in the middle of an outage
        """
        if self._state is self.State.OFFLINE:
            print("END DURING OUTAGE")
            self.report_outage()
            self._state = self.State.UNKNOWN
            self._state_time = None
        elif self._state is self.State.ONLINE:
            self.report_live()
            self._state = self.State.UNKNOWN
            self._state_time = None

    # Both reports contribute the same dataframe with columns
    # ON|OFF start_time duration nb mean stdev min max
    # start_time uses ISO format (YYYY-MM-DDTHH:MM:SS)
    # last 4 columns are only for ON
    def report_outage(self):
        """
        report an outage to the logfile
        """
        duration = round((DateTime.now() - self._state_time).seconds)
        print(f"OFF {self._state_time:%Y/%m/%dT%H:%M:%S}"
              f" {duration}",
              file=self.logfile)

    def report_live(self):
        """
        report a live period to the logfile
        """
        duration = round((DateTime.now() - self._state_time).seconds)
        print(f"ON {self._state_time:%Y/%m/%dT%H:%M:%S} {duration}"
              f" {self.stats.report_line()}",
              file=self.logfile)


def mainloop(context: Context, interval: int,
             output: str, verbose: bool):
    """
    the main loop
    """
    async def _async_mainloop(logfile):
        machine = StateMachine(logfile)
        go_on = True
        def program_killed(*arg):
            nonlocal go_on
            go_on = False
        signal.signal(signal.SIGTERM, program_killed)
        signal.signal(signal.SIGINT, program_killed)
        try:
            while go_on:
                result = await context.ping_once(verbose)
                machine.handle_result(result)
                # print(result)
                await asyncio.sleep(interval)
        finally:
            # print("CLEANUP")
            machine.cleanup()
    with open(output, 'a', encoding='utf-8') as logfile:
        asyncio.run(_async_mainloop(logfile))


def main():
    """
    main entry point
    """
    parser = ArgumentParser(formatter_class=ArgumentDefaultsHelpFormatter)
    parser.add_argument('-l', '--landmark', default='8.8.8.8')
    parser.add_argument('-i', '--iface', default='en0')
    parser.add_argument('-t', '--timeout', default=3)
    parser.add_argument('-p', '--period', default=1)
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument('-o', '--output', default='ping-monitor.log')

    args = parser.parse_args()
    context = Context(args.landmark, args.iface, args.timeout)
    mainloop(context, args.period, args.output, args.verbose)


if __name__ == '__main__':
    main()
