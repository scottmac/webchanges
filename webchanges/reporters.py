"""Runs reports."""

from __future__ import annotations

import asyncio
import difflib
import email.utils
import functools
import getpass
import html
import itertools
import logging
import os
import re
import sys
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, TYPE_CHECKING, Tuple, Type, Union
from warnings import warn

import requests
from markdown2 import Markdown
from requests import Response

from . import __project_name__, __url__, __version__
from .jobs import JobBase, UrlJob
from .mailer import SMTPMailer, SendmailMailer
from .util import TrackSubClasses, chunk_string, linkify

# https://stackoverflow.com/questions/39740632
if TYPE_CHECKING:
    from .handler import JobState, Report

try:
    import aioxmpp
except ImportError:
    aioxmpp = None

try:
    import chump
except ImportError:
    chump = None

try:
    import keyring
except ImportError:
    keyring = None  # type: ignore[assignment]

try:
    import matrix_client.api
except ImportError:
    matrix_client = None

try:
    from pushbullet import Pushbullet
except ImportError:
    Pushbullet = None

if os.name == 'nt':
    try:
        from colorama import AnsiToWin32
    except ImportError:
        AnsiToWin32 = None

logger = logging.getLogger(__name__)


class ReporterBase(object, metaclass=TrackSubClasses):
    __subclasses__: Dict[str, 'ReporterBase'] = {}

    def __init__(self, report: Report, config: Dict[str, Any], job_states: List[JobState], duration: float) -> None:
        self.report = report
        self.config = config
        self.job_states = job_states
        self.duration = duration

    def convert(self, othercls: Type['ReporterBase']) -> 'ReporterBase':
        """Convert to a different ReporterBase class"""
        if hasattr(othercls, '__kind__'):
            config: Dict[Any, str] = self.report.config['report'][othercls.__kind__]
        else:
            config = {}

        return othercls(self.report, config, self.job_states, self.duration)

    @classmethod
    def reporter_documentation(cls) -> str:
        """Return listings of reporters used by --features command line argument"""
        result: List[str] = []
        for sc in TrackSubClasses.sorted_by_kind(cls):
            result.extend((f'  * {sc.__kind__} - {sc.__doc__}',))
        return '\n'.join(result)

    @classmethod
    def submit_one(
        cls,
        name: str,
        report: Report,
        job_states: List[JobState],
        duration: float,
        check_enabled: Optional[bool] = True,
    ) -> None:
        subclass = cls.__subclasses__[name]
        cfg: Dict[str, bool] = report.config['report'].get(name, {'enabled': False})
        if cfg['enabled'] or not check_enabled:
            subclass(report, cfg, job_states, duration).submit()
        else:
            raise ValueError(f'Reporter not enabled: {name}')

    @classmethod
    def submit_all(cls, report: Report, job_states: List[JobState], duration: float) -> None:
        any_enabled = False
        for name, subclass in cls.__subclasses__.items():
            cfg = report.config['report'].get(name, {'enabled': False})
            if cfg['enabled']:
                any_enabled = True
                logger.info(f'Submitting with {name} ({subclass})')
                subclass(report, cfg, job_states, duration).submit()

        if not any_enabled:
            logger.warning('No reporters enabled.')

    def submit(self, **kwargs: Any) -> Iterable[str]:
        raise NotImplementedError()


class HtmlReporter(ReporterBase):
    def submit(self, **kwargs: Any) -> Iterable[str]:
        yield from self._parts()

    def _parts(self) -> Iterable[str]:
        """Generator yielding the HTML; called by submit. Calls _format_content."""
        cfg = self.report.config['report']['html']

        yield f"""
            <!DOCTYPE html>
            <html>
            <head>
            <title>{__project_name__} report</title>
            <meta http-equiv="content-type" content="text/html; charset=utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            </head>
            <body style="font-family:Arial,Helvetica,sans-serif;font-size:13px;">"""

        for job_state in self.report.get_filtered_job_states(self.job_states):
            content = self._format_content(job_state, cfg['diff'])
            if content is not None:
                if hasattr(job_state.job, 'url'):
                    yield (
                        f'<h3>{job_state.verb.title()}: <a href="{html.escape(job_state.job.get_location())}">'
                        f'{html.escape(job_state.job.pretty_name())}</a></h3>'
                    )
                elif job_state.job.pretty_name() != job_state.job.get_location():
                    yield (
                        f'<h3>{job_state.verb.title()}: <span title="{html.escape(job_state.job.get_location())}">'
                        f'{html.escape(job_state.job.pretty_name())}</span></h3>'
                    )
                else:
                    yield f'<h3>{job_state.verb.title()}: {html.escape(job_state.job.get_location())}</h3>'
                if hasattr(job_state.job, 'note') and job_state.job.note:
                    yield f'<h4>{html.escape(job_state.job.note)}</h4>'
                yield content

                yield '<hr>'

        duration = f'{float(f"{self.duration:.2g}"):g}' if self.duration < 10 else f'{self.duration:.0f}'
        yield (
            f"""
            <div style="font-style:italic">
            Checked {len(self.job_states)} source{'s' if len(self.job_states) > 1 else ''} in {duration}
            seconds with <a href="{html.escape(__url__)}">
            {html.escape(__project_name__)}</a></address> {html.escape(__version__)}
            </div>
            </body>
            </html>"""
        )

    @staticmethod
    def _diff_to_html(diff: str, job: JobBase) -> Iterable[str]:
        """Generator yielding the HTML-formatted unified diff; called by _format_content."""
        mark_to_html = None  # PyCharm
        if job.diff_tool and job.diff_tool.startswith('wdiff'):
            # wdiff colorization
            yield '<span style="font-family:monospace;white-space:pre-wrap">'
            diff = html.escape(diff)
            diff = re.sub(
                r'[{][+].*?[+][}]',
                lambda x: f'<span style="background-color:#d1ffd1;color:#082b08">{x.group(0)}</span>',
                diff,
                flags=re.DOTALL,
            )
            diff = re.sub(
                r'[\[][-].*?[-][]]',
                lambda x: (
                    f'<span style="background-color:#fff0f0;color:#9c1c1c;'
                    f'text-decoration:line-through">{x.group(0)}</span>'
                ),
                diff,
                flags=re.DOTALL,
            )
            yield diff
            yield '</span>'
        else:
            if job.is_markdown:
                # rebuild html from markdown using markdown2 library's Markdown
                markdowner = Markdown(safe_mode='escape', extras=['strike', 'target-blank-links'])
                htags = re.compile(r'<(/?)h\d>')
                mtags = re.compile(r'^<p>(<code>)?|(</code>)?</p>$')

                def mark_to_html(text: str) -> str:
                    """converts Markdown (e.g. as generated by html2text filter) back to html"""
                    if text == '* * *':  # manually expand horizontal ruler since <hr> is used to separate jobs
                        return '-' * 80
                    pre = ''
                    post = ''
                    if text.lstrip().startswith('* '):  # item of unordered list
                        lstripped = text.lstrip(' ')
                        indent = len(text) - len(lstripped)
                        pre += '&nbsp;' * indent
                        pre += '● ' if indent == 2 else '⯀ ' if indent == 4 else '○ '
                        text = text.split('* ', 1)[1]
                    elif text.startswith(' '):  # replace leading spaces or converter will strip
                        lstripped = text.lstrip()
                        text = '&nbsp;' * (len(text) - len(lstripped)) + lstripped
                    if job.markdown_padded_tables and '|' in text:
                        # a padded row in a table; keep it monospaced for alignment
                        pre += '<span style="font-family:monospace;white-space:pre-wrap">'
                        post += '</span>'
                    html_out = str(markdowner.convert(text)).strip('\n')  # convert markdown to html
                    html_out = html_out.replace('<a', '<a style="font-family:inherit"')  # fix <a> tag styling
                    html_out = html_out.replace('<img', '<img style="max-width:100%;height:auto;max-height:100%"')
                    html_out, sub = mtags.subn('', html_out)  # remove added tags we don't want
                    if sub:
                        return pre + html_out + post
                    html_out = htags.sub(r'<\g<1>strong>', html_out)  # replace heading tags with <strong>
                    return pre + html_out + post

                yield '<table style="border-collapse:collapse">'
            else:
                yield '<table style="border-collapse:collapse;font-family:monospace">'

            # colors (survive email client in dark mode; contrast ratio https://contrast-ratio.com/ > 7 - AAA level):
            # dark green HSL 120°,  70%, 10% #082b08 on light green HSL 120°, 100%, 91% #d1ffd1
            # dark red   HSL   0°,  70%, 36% #9c1c1c on light red   HSL   0°, 100%, 97% #fff0f0
            # background colors have same relative luminance (.897-.899)
            for i, line in enumerate(diff.splitlines()):
                if line[0] == '+':
                    style = ' style="background-color:#d1ffd1;color:#082b08"' if i > 1 else ' style="color:darkgreen"'
                elif line[0] == '-':
                    style = (
                        ' style="background-color:#fff0f0;color:#9c1c1c;text-decoration:line-through"'
                        if i > 1
                        else ' style="color:darkred"'
                    )
                elif line[0] == '@':  # unified_diff section header
                    style = ' style="background-color:#fbfbfb"'
                elif line[0] == '/':  # informational header added by additions_only or deletions_only filters
                    style = ' style="background-color:lightyellow"'
                else:
                    style = ''
                if i <= 1:
                    style = style[:-1] + ';font-family:monospace"' if style else ' style="font-family:monospace"'
                if i <= 1 or line[0] == '@' or line[0] == '.':  # unified_diff headers or additions_only/deletions_only
                    yield f'<tr{style}><td style="font-family:monospace">{line}</td></tr>'
                else:
                    if job.is_markdown:
                        yield f'<tr{style}><td>{mark_to_html(line[1:])}</td></tr>'
                    else:
                        yield f'<tr{style}><td>{linkify(line[1:])}</td></tr>'
            yield '</table>'

    def _format_content(self, job_state: JobState, difftype: str) -> Optional[str]:
        """Generator yielding the HTML for a job; called by _parts. Calls _diff_to_html."""
        if job_state.verb == 'error':
            return f'<pre style="white-space:pre-wrap;color:red;">{html.escape(job_state.traceback.strip())}</pre>'

        if job_state.verb == 'unchanged':
            return f'<pre style="white-space:pre-wrap">{html.escape(str(job_state.old_data))}</pre>'

        if job_state.old_data in (None, job_state.new_data):
            return '...'

        if difftype == 'unified':
            diff = job_state.get_diff()
            if diff:
                return '\n'.join(self._diff_to_html(diff, job_state.job))
            else:
                return None

        elif difftype == 'table':
            timestamp_old = email.utils.formatdate(job_state.old_timestamp, localtime=True)
            timestamp_new = email.utils.formatdate(time.time(), localtime=True)
            html_diff = difflib.HtmlDiff()
            table = html_diff.make_table(
                str(job_state.old_data).splitlines(keepends=True),
                str(job_state.new_data).splitlines(keepends=True),
                timestamp_old,
                timestamp_new,
                True,
                3,
            )
            table = table.replace('<th ', '<th style="font-family:monospace" ')
            table = table.replace('<td ', '<td style="font-family:monospace" ')
            table = table.replace(' nowrap="nowrap"', '')
            table = table.replace('<a ', '<a style="font-family:monospace;color:inherit" ')
            table = table.replace('<span class="diff_add"', '<span style="color:green;background-color:lightgreen"')
            table = table.replace('<span class="diff_sub"', '<span style="color:red;background-color:lightred"')
            table = table.replace('<span class="diff_chg"', '<span style="color:orange;background-color:lightyellow"')
            return table
        else:
            raise ValueError(f'Diff style not supported: {difftype}')


class TextReporter(ReporterBase):
    def submit(self, **kwargs: Any) -> Iterable[str]:
        cfg = self.report.config['report']['text']
        line_length = cfg['line_length']
        show_details = cfg['details']
        show_footer = cfg['footer']

        if cfg['minimal']:
            for job_state in self.report.get_filtered_job_states(self.job_states):
                pretty_name = job_state.job.pretty_name()
                location = job_state.job.get_location()
                if pretty_name != location:
                    location = f'{pretty_name} ({location})'
                yield ': '.join((job_state.verb.upper(), location))
                if hasattr(job_state.job, 'note'):
                    yield job_state.job.note  # type: ignore[misc]
            return

        summary = []
        details = []
        for job_state in self.report.get_filtered_job_states(self.job_states):
            summary_part, details_part = self._format_output(job_state, line_length)
            summary.extend(summary_part)
            details.extend(details_part)

        if summary:
            sep = (line_length * '=') or None
            yield from (
                part
                for part in itertools.chain(
                    (sep,),
                    (f'{idx + 1:02}. {line}' for idx, line in enumerate(summary)),
                    (sep, ''),
                )
                if part is not None
            )

        if show_details:
            yield from details

        if summary and show_footer:
            duration = f'{float(f"{self.duration:.2g}"):g}' if self.duration < 10 else f'{self.duration:.0f}'
            yield (
                f"--\nChecked {len(self.job_states)} source{'s' if len(self.job_states) > 1 else ''} in {duration}"
                f' seconds with {__project_name__} {__version__}'
            )

    @staticmethod
    def _format_content(job_state: JobState) -> Optional[Union[str, bytes]]:
        if job_state.verb == 'error':
            return job_state.traceback.strip()

        if job_state.verb == 'unchanged':
            return job_state.old_data

        if job_state.old_data in (None, job_state.new_data):
            return None

        return job_state.get_diff()

    def _format_output(self, job_state: JobState, line_length: int) -> Tuple[List[str], List[str]]:
        summary_part: List[str] = []
        details_part: List[str] = []

        pretty_name = job_state.job.pretty_name()
        location = job_state.job.get_location()
        if pretty_name != location:
            location = f'{pretty_name} ({location})'

        pretty_summary = ': '.join((job_state.verb.upper(), pretty_name))
        summary = ': '.join((job_state.verb.upper(), location))
        content = self._format_content(job_state)

        if job_state.verb == 'changed,no_report':
            return [], []

        else:
            summary_part.append(pretty_summary)

            sep = (line_length * '-') or None
            details_part.extend([sep, summary, sep])  # type: ignore[list-item]
            if hasattr(job_state.job, 'note'):
                details_part.extend([job_state.job.note, ''])  # type: ignore[list-item]
            if content is not None:
                details_part.extend([content, sep])  # type: ignore[list-item]
            details_part.extend(
                ['', '']
                if sep
                else [
                    '',
                ]
            )
            details_part = [part for part in details_part if part is not None]

            return summary_part, details_part


class MarkdownReporter(ReporterBase):
    def submit(self, max_length: int = None, **kwargs: Any) -> Iterable[str]:
        cfg = self.report.config['report']['markdown']
        show_details = cfg['details']
        show_footer = cfg['footer']

        if cfg['minimal']:
            for job_state in self.report.get_filtered_job_states(self.job_states):
                pretty_name = job_state.job.pretty_name()
                location = job_state.job.get_location()
                if pretty_name != location:
                    location = f'{pretty_name} ({location})'
                yield f"* {': '.join((job_state.verb.upper(), location))}"
                if hasattr(job_state.job, 'note'):
                    yield job_state.job.note  # type: ignore[misc]
            return

        summary: List[str] = []
        details: List[Tuple[str, str]] = []
        for job_state in self.report.get_filtered_job_states(self.job_states):
            summary_part, details_part = self._format_output(job_state)
            summary.extend(summary_part)
            details.extend(details_part)

        if summary and show_footer:
            duration = f'{float(f"{self.duration:.2g}"):g}' if self.duration < 10 else f'{self.duration:.0f}'
            footer = (
                f"--\nChecked {len(self.job_states)} source{'s' if len(self.job_states) > 1 else ''} in"
                f' {duration} seconds with {__project_name__} {__version__}'
            )
        else:
            footer = ''

        trimmed_msg = '*Parts of the report were omitted due to message length.*\n'
        if max_length:
            max_length -= len(trimmed_msg)

        trimmed, summary, details, footer = self._render(max_length, summary, details, footer)

        if summary:
            yield from summary
            yield ''

        if show_details:
            for header, body in details:
                yield header
                yield body
                yield ''

        if trimmed:
            yield trimmed_msg

        if summary and show_footer:
            yield footer

    @classmethod
    def _render(
        cls, max_length: Optional[int], summary: List[str], details: List[Tuple[str, str]], footer: str
    ) -> Tuple[bool, List[str], List[Tuple[str, str]], str]:
        """Render the report components, trimming them if the available length is insufficient.

        :returns: a tuple (trimmed, summary, details, footer).

        The first element of the tuple indicates whether any part of the report
        was omitted due to message length. The other elements are the
        potentially trimmed report components.
        """

        # The footer/summary lengths are the sum of the length of their parts
        # plus the space taken up by newlines.
        if summary:
            summary = [f'{idx + 1}. {line}' for idx, line in enumerate(summary)]
            summary_len = sum(len(part) for part in summary) + len(summary) - 1
        else:
            summary_len = 0

        footer_len = sum(len(part) for part in footer) + len(footer) - 1

        if max_length is None:
            processed_details: List[Tuple[str, str]] = []
            for header, body in details:
                _, body = cls._format_details_body(body)
                processed_details.append((header, body))
            return False, summary, processed_details, footer
        else:
            if summary_len > max_length:
                return True, [], [], ''
            elif footer_len > max_length - summary_len:
                return True, summary, [], footer[: max_length - summary_len]
            elif not details:
                return False, summary, [], footer
            else:
                # Determine the space remaining after taking into account summary and footer.
                remaining_len = max_length - summary_len - footer_len
                headers_len = sum(len(header) for header, _ in details)

                details_trimmed = False

                # First ensure we can show all the headers.
                if headers_len > remaining_len:
                    return True, summary, [], footer
                else:
                    remaining_len -= headers_len

                    # Calculate approximate available length per item, shared equally between all details components.
                    body_len_per_details = remaining_len // len(details)

                    trimmed_details: List[Tuple[str, str]] = []
                    unprocessed = len(details)

                    for header, body in details:
                        # Calculate the available length for the body and render it
                        avail_length = body_len_per_details - 1

                        body_trimmed, body = cls._format_details_body(body, avail_length)

                        if body_trimmed:
                            details_trimmed = True

                        if len(body) <= body_len_per_details:
                            trimmed_details.append((header, body))
                        else:
                            trimmed_details.append((header, ''))

                        # If the current item's body did not use all of its allocated space, distribute the unused space
                        # into subsequent items, unless we're at the last item already.
                        unused = body_len_per_details - len(body)
                        remaining_len -= body_len_per_details
                        remaining_len += unused
                        unprocessed -= 1

                        if unprocessed > 0:
                            body_len_per_details = remaining_len // unprocessed

                    return details_trimmed, summary, trimmed_details, footer

    @staticmethod
    def _format_details_body(s: str, max_length: Optional[int] = None) -> Tuple[bool, str]:

        if any(s[:3] == x for x in ('+++', '---', '...')):  # is a unified diff (with our '...' modification)
            lines = s.splitlines()
            for i in range(len(lines)):
                if i <= 1 or lines[i][:3] == '@@ ':
                    lines[i] = f'`{lines[i]}`'
            s = '\n'.join(lines)

        # Message to print when the diff is too long.
        trim_message = '*diff trimmed*\n'
        trim_message_length = len(trim_message)

        if max_length is None or len(s) <= max_length:
            return False, s
        else:
            target_max_length = max_length - trim_message_length
            pos = s.rfind('\n', 0, target_max_length)

            if pos == -1:
                # Just a single long line, so cut it short.
                s = s[0:target_max_length]
            else:
                # Multiple lines, cut off extra lines.
                s = s[0:pos]

            return True, f'{trim_message}{s}'

    @staticmethod
    def _format_content(job_state: JobState) -> Optional[Optional[Union[str, bytes]]]:
        if job_state.verb == 'error':
            return job_state.traceback.strip()

        if job_state.verb == 'unchanged':
            return job_state.old_data

        if job_state.old_data in (None, job_state.new_data):
            return None

        return job_state.get_diff()

    def _format_output(self, job_state: JobState) -> Tuple[List[str], List[Tuple[str, str]]]:
        summary_part: List[str] = []
        details_part: List[Tuple[str, str]] = []

        pretty_name = job_state.job.pretty_name()
        location = job_state.job.get_location()
        if pretty_name != location:
            if isinstance(job_state.job, UrlJob):
                location = f'[{pretty_name}]({location})'
            else:
                location = f'{pretty_name} ({location})'

        pretty_summary = ': '.join((job_state.verb.upper(), pretty_name))
        summary = ': '.join((job_state.verb.upper(), location))
        content = self._format_content(job_state)

        if job_state.verb == 'changed,no_report':
            return [], []

        else:
            summary_part.append(pretty_summary)

            if content is not None:
                details_part.append(('### ' + summary, content))

            return summary_part, details_part


class StdoutReporter(TextReporter):
    """Print summary on stdout (the console)."""

    __kind__ = 'stdout'

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._has_color = sys.stdout.isatty() and self.config.get('color', False)

    def _incolor(self, color_id: int, s: str) -> str:
        if self._has_color:
            return f'\033[9{color_id}m{s}\033[0m'
        return s

    def _red(self, s: str) -> str:
        return self._incolor(1, s)

    def _green(self, s: str) -> str:
        return self._incolor(2, s)

    def _yellow(self, s: str) -> str:
        return self._incolor(3, s)

    def _blue(self, s: str) -> str:
        return self._incolor(4, s)

    def _get_print(self) -> Callable:
        if os.name == 'nt' and self._has_color and AnsiToWin32 is not None:
            return functools.partial(print, file=AnsiToWin32(sys.stdout).stream)
        return print

    def submit(self, **kwargs: Any) -> None:  # type: ignore[override]
        print_color = self._get_print()

        cfg = self.report.config['report']['text']
        line_length = cfg['line_length']

        separators = (line_length * '=', line_length * '-', '--') if line_length else ()
        body = '\n'.join(super().submit())

        if any(
            diff_tool.startswith('wdiff')
            for diff_tool in (job_state.job.diff_tool for job_state in self.job_states if job_state.job.diff_tool)
        ):
            # wdiff colorization
            body = re.sub(r'[{][+].*?[+][}]', lambda x: self._green(x.group(0)), body, flags=re.DOTALL)
            body = re.sub(r'[\[][-].*?[-][]]', lambda x: self._red(x.group(0)), body, flags=re.DOTALL)
            separators = (*separators, '-' * 36)

        for line in body.splitlines():
            if line in separators:
                print_color(line)
            elif line.startswith('+'):
                print_color(self._green(line))
            elif line.startswith('-'):
                print_color(self._red(line))
            elif any(line.startswith(prefix) for prefix in ('NEW: ', 'CHANGED: ', 'UNCHANGED: ', 'ERROR: ')):
                first, second = line.split(' ', 1)
                if line.startswith('ERROR: '):
                    print_color(first, self._red(second))
                else:
                    print_color(first, self._blue(second))
            else:
                print_color(line)


class EMailReporter(TextReporter):
    """Send summary via e-mail (including SMTP)."""

    __kind__ = 'email'

    def submit(self, **kwargs: Any) -> None:  # type: ignore[override]

        body_text = '\n'.join(super().submit())

        if not body_text:
            logger.debug(f'Reporter {self.__kind__} has nothing to report; execution aborted')
            return

        filtered_job_states = list(self.report.get_filtered_job_states(self.job_states))
        subject_args = {
            'count': len(filtered_job_states),
            'jobs': ', '.join(job_state.job.pretty_name() for job_state in filtered_job_states),
        }
        subject = self.config['subject'].format(**subject_args)

        if self.config['method'] == 'smtp':
            smtp_user = self.config['smtp'].get('user', None) or self.config['from']
            use_auth = self.config['smtp'].get('auth', False)
            mailer: Union[SMTPMailer, SendmailMailer] = SMTPMailer(
                smtp_user,
                self.config['smtp']['host'],
                self.config['smtp']['port'],
                self.config['smtp']['starttls'],
                use_auth,
                self.config['smtp'].get('insecure_password'),
            )
        elif self.config['method'] == 'sendmail':
            mailer = SendmailMailer(self.config['sendmail']['path'])
        else:
            raise ValueError(f"Unknown email reporter method: {self.config['method']}")

        if self.config['html']:
            html_reporter = HtmlReporter(self.report, {}, self.job_states, self.duration)
            body_html = '\n'.join(html_reporter.submit())
            msg = mailer.msg(self.config['from'], self.config['to'], subject, body_text, body_html)
        else:
            msg = mailer.msg(self.config['from'], self.config['to'], subject, body_text)

        mailer.send(msg)


class IFTTTReport(TextReporter):
    """Send summary via IFTTT."""

    __kind__ = 'ifttt'

    def submit(self, **kwargs: Any) -> None:  # type: ignore[override]
        webhook_url = 'https://maker.ifttt.com/trigger/{event}/with/key/{key}'.format(**self.config)
        for job_state in self.report.get_filtered_job_states(self.job_states):
            pretty_name = job_state.job.pretty_name()
            location = job_state.job.get_location()
            _ = requests.post(
                webhook_url,
                json={
                    'value1': job_state.verb,
                    'value2': pretty_name,
                    'value3': location,
                },
            )


class WebServiceReporter(TextReporter):
    """Base class for other reporters, such as Pushover and Pushbullet."""

    __kind__ = ''
    MAX_LENGTH = 1024

    def web_service_get(self) -> str:
        raise NotImplementedError

    def web_service_submit(self, service: str, title: str, body: str) -> None:
        raise NotImplementedError

    def submit(self, **kwargs: Any) -> None:  # type: ignore[override]
        text = '\n'.join(super().submit())

        if not text:
            logger.debug(f'Not sending {self.__kind__} (no changes)')
            return

        if len(text) > self.MAX_LENGTH:
            text = text[: self.MAX_LENGTH]

        try:
            service = self.web_service_get()
        except Exception as e:
            raise RuntimeError(
                f'Failed to load or connect to {self.__kind__} - are the dependencies installed and ' 'configured?'
            ) from e

        self.web_service_submit(service, 'Website Change Detected', text)


class PushoverReport(WebServiceReporter):
    """Send summary via pushover.net."""

    __kind__ = 'pushover'

    def web_service_get(self) -> 'chump.Application':
        if chump is None:
            raise ImportError('Python module "chump" not installed')

        app = chump.Application(self.config['app'])
        return app.get_user(self.config['user'])

    def web_service_submit(self, service: 'chump.Application', title: str, body: str) -> None:
        sound = self.config['sound']
        # If device is the empty string or not specified at all, use None to send to all devices
        # (see https://github.com/thp/urlwatch/issues/372)
        device = self.config.get('device', None) or None
        priority = {
            'lowest': chump.LOWEST,
            'low': chump.LOW,
            'normal': chump.NORMAL,
            'high': chump.HIGH,
            'emergency': chump.EMERGENCY,
        }.get(self.config.get('priority', None), chump.NORMAL)
        msg = service.create_message(
            title=title, message=body, html=True, sound=sound, device=device, priority=priority
        )
        msg.send()


class PushbulletReport(WebServiceReporter):
    """Send summary via pushbullet.com."""

    __kind__ = 'pushbullet'

    def web_service_get(self) -> 'Pushbullet':
        if Pushbullet is None:
            raise ImportError('Python module "pushbullet" not installed')

        return Pushbullet(self.config['api_key'])

    def web_service_submit(self, service: 'Pushbullet', title: str, body: str) -> None:
        service.push_note(title, body)


class MailGunReporter(TextReporter):
    """Send e-mail via the Mailgun service."""

    __kind__ = 'mailgun'

    def submit(self) -> None:  # type: ignore[override]
        region = self.config.get('region', '')
        domain = self.config['domain']
        api_key = self.config['api_key']
        from_name = self.config['from_name']
        from_mail = self.config['from_mail']
        to = self.config['to']

        if region == 'us':
            region = ''

        if region != '':
            region = f'.{region}'

        body_text = '\n'.join(super().submit())
        body_html = '\n'.join(self.convert(HtmlReporter).submit())

        if not body_text:
            logger.debug(f'Reporter {self.__kind__} has nothing to report; execution aborted')
            return None

        filtered_job_states = list(self.report.get_filtered_job_states(self.job_states))
        subject_args = {
            'count': len(filtered_job_states),
            'jobs': ', '.join(job_state.job.pretty_name() for job_state in filtered_job_states),
        }
        subject = self.config['subject'].format(**subject_args)

        logger.debug(f"Sending Mailgun request for domain:'{domain}'")
        result = requests.post(
            f'https://api{region}.mailgun.net/v3/{domain}/messages',
            auth=('api', api_key),
            data={
                'from': f'{from_name} <{from_mail}>',
                'to': to,
                'subject': subject,
                'text': body_text,
                'html': body_html,
            },
        )

        try:
            json_res = result.json()

            if result.status_code == requests.codes.ok:
                logger.info(f"Mailgun response: id '{json_res['id']}'. {json_res['message']}")
            else:
                raise RuntimeError(f"Mailgun error: {json_res['message']}")
        except ValueError:
            raise RuntimeError(
                f'Failed to parse Mailgun response. HTTP status code: {result.status_code}, content: {result.text}'
            )


class TelegramReporter(MarkdownReporter):
    """Send a Markdown message using Telegram.

    See https://core.telegram.org/bots/api#formatting-options
    """

    __kind__ = 'telegram'

    def submit(self, max_length: int = 4096) -> None:  # type: ignore[override]
        """Submit report."""
        bot_token = self.config['bot_token']
        chat_ids = self.config['chat_id']
        chat_ids = [chat_ids] if not isinstance(chat_ids, list) else chat_ids

        text = '\n'.join(super().submit())  # no max_length here as we will chunk later

        if not text:
            logger.debug(f'Reporter {self.__kind__} has nothing to report; execution aborted')
            return None

        chunks = self.telegram_chunk_by_line(text, max_length)

        for chat_id in chat_ids:
            for chunk in chunks:
                self.submit_to_telegram(bot_token, chat_id, chunk)

    def submit_to_telegram(self, bot_token: str, chat_id: Union[int, str], text: str) -> Response:
        """Submit to Telegram."""
        logger.info(f"Sending telegram message to chat id: '{chat_id}'")

        data = {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'MarkdownV2',
            'disable_web_page_preview': True,
            'disable_notification': self.config['silent'],
        }
        result = requests.post(f'https://api.telegram.org/bot{bot_token}/sendMessage', data=data)

        try:
            json_res = result.json()

            if result.status_code == requests.codes.ok:
                logger.info(f"Telegram response: ok '{json_res['ok']}'. {json_res['result']}")
            else:
                raise RuntimeError(f"Telegram error: {json_res['description']}")
        except ValueError:
            logger.error(
                f'Failed to parse telegram response. HTTP status code: {result.status_code}, content:'
                f' {result.content}'  # type: ignore[str-bytes-safe]
            )

        return result

    @staticmethod
    def telegram_escape_markdown(text: str, version: int = 2, entity_type: Optional[str] = None) -> str:
        """
        Helper function to escape telegram markup symbols. See https://core.telegram.org/bots/api#formatting-options

        Inspired by https://github.com/python-telegram-bot/python-telegram-bot/blob/master/telegram/utils/helpers.py
        v13.5 30-Apr-21

        :param text: The text.
        :param version: Use to specify the version of telegrams Markdown. Either ``1`` or ``2``. Defaults to ``2``.
        :param entity_type: For the entity types ``pre``, ``code`` and the link part of ``text_links``, only certain
            characters need to be escaped in ``MarkdownV2``. See the official API documentation for details. Only valid
            in combination with ``version=2``, will be ignored otherwise.

        :returns: The escaped text.
        """
        if version == 1:
            escape_chars = r'_*`['
        elif version == 2:
            if entity_type is None:
                escape_chars = r'\_*[]()~`>#+-=|{}.!'
            elif entity_type in ('pre', 'code'):
                escape_chars = r'\`'
            elif entity_type == 'text_link':
                escape_chars = r'\)[]'
            else:
                raise ValueError("entity_type must be None, 'pre', 'code' or 'text_link'!")
        else:
            raise ValueError('Markdown version must be either 1 or 2!')

        text = re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)
        lines = text.splitlines(keepends=True)
        for i in range(len(lines)):
            if lines[i].count('\\*\\*') and not lines[i].count('\\*\\*') % 2:
                lines[i] = lines[i].replace('\\*\\*', '*')  # rebuild bolding from html2text
            if lines[i].count('\\~\\~') and not lines[i].count('\\~\\~') % 2:
                lines[i] = lines[i].replace('\\~\\~', '~')  # rebuild strikethrough from html2text

        return ''.join(lines)

    def telegram_chunk_by_line(self, text: str, max_length: int) -> List[str]:
        """Chunk-ify by line while escaping markdown as required by Telegram."""
        chunks = []

        # Escape Markdown by type
        lines = re.split(r'(`)(.*?)(`)', text)
        for i in range(len(lines)):
            if i % 4 in (1, 3):
                continue
            base_entity = 'code' if i % 4 == 2 else None
            # subtext = re.split(r'(\[[^\][]*]\((?:https?|tel|mailto):[^()]*\))', text[i])
            subtext = re.split(r'(\[.*?]\((?:https?|tel|mailto):[^()]*\))', lines[i])
            for j in range(len(subtext)):
                if (j + 1) % 2:
                    subtext[j] = self.telegram_escape_markdown(subtext[j], entity_type=base_entity)
                else:
                    subtext[j] = ''.join(
                        [
                            '[',
                            self.telegram_escape_markdown(subtext[j][1:].split(']')[0]),
                            '](',
                            self.telegram_escape_markdown(
                                subtext[j].split('(')[-1].split(')')[0], entity_type='text_link'
                            ),
                            ')',
                        ]
                    )
            lines[i] = ''.join(subtext)

        new_text = ''.join(lines).splitlines(keepends=True)

        # check if any individual line is too long and chunk it
        if any(len(line) > max_length for line in new_text):
            new_lines: List[str] = []
            for line in new_text:
                if len(line) > max_length:
                    new_lines.extend(chunk_string(line, max_length))
                else:
                    new_lines.append(line)
            new_text = new_lines

        it_lines = iter(new_text)
        chunk_lines: List[str] = []
        pre_status = False  # keep track of whether you're in the middle of a PreCode entity to close and reopen
        try:
            while True:
                next_line = next(it_lines)
                if sum(len(line) for line in chunk_lines) + len(next_line) > max_length - pre_status * 3:
                    if pre_status:
                        chunk_lines[-1] += '```'
                    chunks.append(''.join(chunk_lines))
                    chunk_lines = [pre_status * '```' + next_line]
                else:
                    chunk_lines.append(next_line)
                if next_line.count('```') % 2:
                    pre_status = not pre_status
        except StopIteration:
            chunks.append(''.join(chunk_lines))

        return chunks


class WebhookReporter(TextReporter):
    """Send a text message to a webhook such as Slack or Discord channel."""

    __kind__ = 'webhook'

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        default_max_length = 2000 if self.config['webhook_url'][:23] == 'https://discordapp.com/' else 40000
        if isinstance(self.config.get('max_message_length'), int):
            self.max_length = int(self.config.get('max_message_length'))  # type: ignore[arg-type]
        else:
            self.max_length = default_max_length

    def submit(self) -> Optional[Response]:  # type: ignore[override]
        webhook_url = self.config['webhook_url']
        text = '\n'.join(super().submit())

        if not text:
            logger.debug(f'Reporter {self.__kind__} has nothing to report; execution aborted')
            return None

        result = None
        for chunk in chunk_string(text, self.max_length, numbering=True):
            res = self.submit_to_webhook(webhook_url, chunk)
            if res.status_code != requests.codes.ok or res is None:
                result = res

        return result

    @staticmethod
    def submit_to_webhook(webhook_url: str, text: str) -> Response:
        logger.debug(f'Sending request to webhook with text:{text}')
        post_data = {'text': text}
        result = requests.post(webhook_url, json=post_data)
        try:
            if result.status_code == requests.codes.ok:
                logger.info('Webhook server response: ok')
            else:
                raise RuntimeError(f'Webhook server error: {result.text}')
        except ValueError:
            logger.error(
                f'Failed to parse webhook server response. HTTP status code: {result.status_code}, content:'
                f' {result.content}'  # type: ignore[str-bytes-safe]
            )
        return result


class SlackReporter(WebhookReporter):
    """Deprecated; use webhook instead."""

    __kind__ = 'slack'

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        warn("'slack' reporter is deprecated; replace with 'webhook' (same exact keys)", DeprecationWarning)
        super().__init__(*args, **kwargs)


class WebhookMarkdownReporter(MarkdownReporter):
    """Send a Markdown message to a webhook such as a Mattermost channel."""

    __kind__ = 'webhook_markdown'

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        default_max_length = 2000 if self.config['webhook_url'][:23] == 'https://discordapp.com/' else 40000
        if isinstance(self.config.get('max_message_length'), int):
            self.max_length: int = self.config.get('max_message_length')
        else:
            self.max_length = default_max_length

    def submit(self) -> None:  # type: ignore[override]
        webhook_url = self.config['webhook_url']
        text = '\n'.join(super().submit())

        if not text:
            logger.debug(f'Reporter {self.__kind__} has nothing to report; execution aborted')
            return None

        for chunk in chunk_string(text, self.max_length, numbering=True):
            self.submit_to_webhook(webhook_url, chunk)

    @staticmethod
    def submit_to_webhook(webhook_url: str, text: str) -> requests.Response:
        logger.debug(f'Sending request to webhook_markdown with text:{text}')
        post_data = {'text': text}
        result = requests.post(webhook_url, json=post_data)
        try:
            if result.status_code == requests.codes.ok:
                logger.info('Webhook_markdown server response: ok')
            else:
                raise RuntimeError(f'Webhook_markdown server error: {result.text}')
        except ValueError:
            logger.error(
                f'Failed to parse webhook_markdown server response. HTTP status code: {result.status_code}, '
                f'content: {result.content}'  # type: ignore[str-bytes-safe]
            )
        return result


class MatrixReporter(MarkdownReporter):
    """Send a message to a room using the Matrix protocol."""

    MAX_LENGTH = 16384

    __kind__ = 'matrix'

    def submit(self, max_length: int = None) -> None:  # type: ignore[override]
        if matrix_client is None:
            raise ImportError('Python module "matrix_client" not installed')

        homeserver_url = self.config['homeserver']
        access_token = self.config['access_token']
        room_id = self.config['room_id']

        body_markdown = '\n'.join(super().submit(self.MAX_LENGTH))

        if not body_markdown:
            logger.debug(f'Reporter {self.__kind__} has nothing to report; execution aborted')
            return

        client_api = matrix_client.api.MatrixHttpApi(homeserver_url, access_token)

        body_html = Markdown(extras=['fenced-code-blocks', 'highlightjs-lang']).convert(body_markdown)

        try:
            client_api.send_message_event(
                room_id,
                'm.room.message',
                content={
                    'msgtype': 'm.text',
                    'format': 'org.matrix.custom.html',
                    'body': body_markdown,
                    'formatted_body': body_html,
                },
            )
        except Exception as e:
            raise RuntimeError(f'Matrix error: {e}')


class XMPPReporter(TextReporter):
    """Send a message using the XMPP Protocol."""

    MAX_LENGTH = 262144

    __kind__ = 'xmpp'

    def submit(self) -> None:  # type: ignore[override]

        sender = self.config['sender']
        recipient = self.config['recipient']

        text = '\n'.join(super().submit())

        if not text:
            logger.debug(f'Reporter {self.__kind__} has nothing to report; execution aborted')
            return

        xmpp = XMPP(sender, recipient, self.config.get('insecure_password'))

        for chunk in chunk_string(text, self.MAX_LENGTH, numbering=True):
            asyncio.run(xmpp.send(chunk))


class BrowserReporter(HtmlReporter):
    """Display HTML summary using the default web browser."""

    __kind__ = 'browser'

    def submit(self) -> None:  # type: ignore[override]
        filtered_job_states = list(self.report.get_filtered_job_states(self.job_states))
        if not filtered_job_states:
            logger.debug(f'Reporter {self.__kind__} has nothing to report; execution aborted')
            return

        html_reporter = HtmlReporter(self.report, {}, self.job_states, self.duration)
        body_html = '\n'.join(html_reporter.submit())

        # recheck after running as diff_filters can modify job_states.verb
        filtered_job_states = list(self.report.get_filtered_job_states(self.job_states))
        if not filtered_job_states:
            logger.debug(f'Reporter {self.__kind__} has nothing to report; execution aborted')
            return

        import tempfile
        import webbrowser

        f = tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False)
        f.write(body_html)
        f.close()
        webbrowser.open(f.name)
        time.sleep(2)
        os.remove(f.name)


class XMPP(object):
    def __init__(self, sender: str, recipient: str, insecure_password: str = None) -> None:
        if aioxmpp is None:
            raise ImportError('Python package "aioxmpp" is not installed; cannot use the "xmpp" reporter')

        self.sender = sender
        self.recipient = recipient
        self.insecure_password = insecure_password

    async def send(self, chunk: str) -> None:
        if self.insecure_password:
            password = self.insecure_password
        elif keyring is not None:
            passw = keyring.get_password('urlwatch_xmpp', self.sender)
            if passw is None:
                raise ValueError(f'No password available in keyring for {self.sender}')
            else:
                password = passw
        else:
            raise ValueError(f'No password available for {self.sender}')

        jid = aioxmpp.JID.fromstr(self.sender)
        client = aioxmpp.PresenceManagedClient(jid, aioxmpp.make_security_layer(password))
        recipient_jid = aioxmpp.JID.fromstr(self.recipient)

        async with client.connected() as stream:
            msg = aioxmpp.Message(
                to=recipient_jid,
                type_=aioxmpp.MessageType.CHAT,
            )
            msg.body[None] = chunk

            await stream.send_and_wait_for_sent(msg)


def xmpp_have_password(sender: str) -> bool:
    if keyring is None:
        raise ImportError('Python package "keyring" is non installed - service unsupported')

    return keyring.get_password('urlwatch_xmpp', sender) is not None


def xmpp_set_password(sender: str) -> None:
    """Set the keyring password for the XMPP connection. Interactive."""
    if keyring is None:
        raise ImportError('Python package "keyring" is non installed - service unsupported')

    password = getpass.getpass(prompt=f'Enter password for {sender}: ')
    keyring.set_password('urlwatch_xmpp', sender, password)


class ProwlReporter(TextReporter):
    """Send a detailed notification via prowlapp.com."""

    # contributed by nitz https://github.com/thp/urlwatch/pull/633

    __kind__ = 'prowl'

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)

    def submit(self) -> None:  # type: ignore[override]
        api_add = 'https://api.prowlapp.com/publicapi/add'

        text = '\n'.join(super().submit())

        if not text:
            logger.debug(f'Reporter {self.__kind__} has nothing to report; execution aborted')
            return None

        filtered_job_states = list(self.report.get_filtered_job_states(self.job_states))
        subject_args = {
            'count': len(filtered_job_states),
            'jobs': ', '.join(job_state.job.pretty_name() for job_state in filtered_job_states),
        }

        # 'subject' used in the config file, but the API
        # uses what might be called the subject as the 'event'
        event = self.config['subject'].format(**subject_args)

        # 'application' is prepended to the message in prowl,
        # to show the source of the notification. this too,
        # is user configurable, and may reference subject args
        application = self.config.get('application')
        if application is not None:
            application = application.format(**subject_args)
        else:
            application = f'{__project_name__} v{__version__}'

        # build the data to post
        post_data = {
            'event': event[:1024],
            'description': text[:10000],
            'application': application[:256],
            'apikey': self.config['api_key'],
            'priority': self.config['priority'],
        }

        # all set up, add the notification!
        result = requests.post(api_add, data=post_data)

        try:
            if result.status_code in (requests.codes.ok, requests.codes.no_content):
                logger.info('Prowl response: ok')
            else:
                raise RuntimeError(f'Prowl error: {result.text}')
        except ValueError:
            logger.error(
                f'Failed to parse Prowl response. HTTP status code: {result.status_code},'
                f' content: {result.content}'  # type: ignore[str-bytes-safe]
            )
