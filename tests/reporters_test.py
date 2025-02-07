"""Test reporting, primarily handling of diffs."""
import importlib.util
import logging
import os
import traceback

import pytest

from keyring.errors import NoKeyringError

from requests.exceptions import MissingSchema

from webchanges.handler import JobState, Report
from webchanges.jobs import JobBase
from webchanges.mailer import smtp_have_password, smtp_set_password
from webchanges.reporters import HtmlReporter
from webchanges.storage import DEFAULT_CONFIG

logger = logging.getLogger(__name__)

matrix_client_is_installed = importlib.util.find_spec('matrix_client') is not None
xmpp_is_installed = importlib.util.find_spec('xmpp') is not None

DIFF_TO_HTML_TEST_DATA = [
    ('+Added line', '<tr style="background-color:#d1ffd1;color:#082b08"><td>Added line</td></tr>'),
    (
        '-Deleted line',
        '<tr style="background-color:#fff0f0;color:#9c1c1c;text-decoration:line-through">' '<td>Deleted line</td></tr>',
    ),
    # Changes line
    (
        '@@ -1,1 +1,1 @@',
        '<tr style="background-color:#fbfbfb"><td style="font-family:monospace">@@ -1,1 +1,1 @@' '</td></tr>',
    ),
    # Horizontal ruler is manually expanded since <hr> tag is used to separate jobs
    (
        '+* * *',
        '<tr style="background-color:#d1ffd1;color:#082b08"><td>'
        '--------------------------------------------------------------------------------</td></tr>',
    ),
    (
        '+[Link](https://example.com)',
        '<tr style="background-color:#d1ffd1;color:#082b08"><td><a style="font-family:inherit" rel="noopener" '
        'target="_blank" href="https://example.com">Link</a></td></tr>',
    ),
    (
        ' ![Image](https://example.com/picture.png "picture")',
        '<tr><td><img style="max-width:100%;height:auto;max-height:100%" src="https://example.com/picture.png"'
        ' alt="Image" title="picture" /></td></tr>',
    ),
    (
        '   Indented text (replace leading spaces)',
        '<tr><td>&nbsp;&nbsp;Indented text (replace leading spaces)</td></tr>',
    ),
    (' # Heading level 1', '<tr><td><strong>Heading level 1</strong></td></tr>'),
    (' ## Heading level 2', '<tr><td><strong>Heading level 2</strong></td></tr>'),
    (' ### Heading level 3', '<tr><td><strong>Heading level 3</strong></td></tr>'),
    (' #### Heading level 4', '<tr><td><strong>Heading level 4</strong></td></tr>'),
    (' ##### Heading level 5', '<tr><td><strong>Heading level 5</strong></td></tr>'),
    (' ###### Heading level 6', '<tr><td><strong>Heading level 6</strong></td></tr>'),
    ('   * Bullet point level 1', '<tr><td>&nbsp;&nbsp;● Bullet point level 1</td></tr>'),
    ('     * Bullet point level 2', '<tr><td>&nbsp;&nbsp;&nbsp;&nbsp;⯀ Bullet point level 2</td></tr>'),
    ('       * Bullet point level 3', '<tr><td>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;○ Bullet point level 3</td></tr>'),
    (
        '         * Bullet point level 4',
        '<tr><td>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;○ Bullet point level 4</td></tr>',
    ),
    (' *emphasis*', '<tr><td><em>emphasis</em></td></tr>'),
    (' _**emphasis and strong**_', '<tr><td><em><strong>emphasis and strong</strong></em></td></tr>'),
    (' **strong**', '<tr><td><strong>strong</strong></td></tr>'),
    (' **_strong and emphasis_**', '<tr><td><strong><em>strong and emphasis</em></strong></td></tr>'),
    (' ~~strikethrough~~', '<tr><td><strike>strikethrough</strike></td></tr>'),
    (' | table | row |', '<tr><td>| table | row |</td></tr>'),
]

ALL_REPORTERS = [
    reporter for reporter, v in DEFAULT_CONFIG['report'].items() if reporter not in ('html', 'text', 'markdown')
]


class UrlwatchTest:
    class config_storage:
        config = DEFAULT_CONFIG


report = Report(UrlwatchTest)


@pytest.mark.parametrize('inpt, out', DIFF_TO_HTML_TEST_DATA)
def test_diff_to_html(inpt, out):
    # must add to fake headers to get what we want:
    inpt = '-fake head 1\n+fake head 2\n' + inpt
    job = JobBase.unserialize({'url': 'https://www.example.com', 'is_markdown': True, 'markdown_padded_tables': False})
    result = ''.join(list(HtmlReporter(report, {}, '', 0)._diff_to_html(inpt, job)))
    assert result[250:-8] == out


def test_diff_to_htm_padded_table():
    # must add to fake headers to get what we want:
    inpt = '-fake head 1\n+fake head 2\n | table | row |'
    job = JobBase.unserialize({'url': 'https://www.example.com', 'is_markdown': True, 'markdown_padded_tables': True})
    result = ''.join(list(HtmlReporter(report, {}, '', 0)._diff_to_html(inpt, job)))
    assert result[250:-8] == (
        '<tr><td><span style="font-family:monospace;white-space:pre-wrap">| table | ' 'row |</span></td></tr>'
    )


def test_diff_to_htm_wdiff():
    # must add to fake headers to get what we want:
    inpt = '[-old-]{+new+}'
    job = JobBase.unserialize(
        {'url': 'https://www.example.com', 'is_markdown': False, 'markdown_padded_tables': False, 'diff_tool': 'wdiff'}
    )
    result = ''.join(list(HtmlReporter(report, {}, '', 0)._diff_to_html(inpt, job)))
    assert result == (
        '<span style="font-family:monospace;white-space:pre-wrap">'
        '<span style="background-color:#fff0f0;color:#9c1c1c;text-decoration:line-through">[-old-]</span>'
        '<span style="background-color:#d1ffd1;color:#082b08">{+new+}</span></span>'
    )


def test_smtp_password():
    try:
        assert smtp_have_password('fdsfdsfdsafdsf', '') is False
    except NoKeyringError:
        pass
    with pytest.raises((OSError, ImportError, NoKeyringError)):
        smtp_set_password('', '')


@pytest.mark.parametrize('reporter', ALL_REPORTERS)
def test_reporters(reporter):
    def build_job(name, url, old, new):
        job = JobBase.unserialize({'name': name, 'url': url})

        # Can pass in None as cache_storage, as we are not
        # going to load or save the job state for testing;
        # also no need to use it as context manager, since
        # no processing is called on the job
        job_state = JobState(None, job)

        job_state.old_data = old
        job_state.new_data = new

        return job_state

    def set_error(job_state, message):
        try:
            raise ValueError(message)
        except ValueError as e:
            job_state.exception = e
            job_state.traceback = job_state.job.format_error(e, traceback.format_exc())

        return job_state

    report.new(build_job('Newly Added', 'https://example.com/new', '', ''))
    report.changed(
        build_job(
            'Something Changed',
            'https://example.com/changed',
            """
    Unchanged Line
    Previous Content
    Another Unchanged Line
    """,
            """
    Unchanged Line
    Updated Content
    Another Unchanged Line
    """,
        )
    )
    report.changed_no_report(build_job('Newly Added', 'https://example.com/changed_no_report', '', ''))
    report.unchanged(
        build_job('Same As Before', 'https://example.com/unchanged', 'Same Old, Same Old\n', 'Same Old, Same Old\n')
    )
    report.error(set_error(build_job('Error Reporting', 'https://example.com/error', '', ''), 'Sample error text'))

    if reporter == 'email':
        with pytest.raises((ValueError, NoKeyringError)) as pytest_wrapped_e:
            report.finish_one(reporter, check_enabled=False)
        assert any(
            x in str(pytest_wrapped_e.value)
            for x in (
                'No password available in keyring for localhost ',
                'No password available for localhost ',
                'No recommended backend was available.',
            )
        )
    elif reporter == 'xmpp':
        if not xmpp_is_installed:
            logger.warning(f"Skipping {reporter} since 'aioxmpp' package is not installed")
            return
        else:
            with pytest.raises((ValueError, NoKeyringError)) as pytest_wrapped_e:
                report.finish_one(reporter, check_enabled=False)
            assert any(
                x in str(pytest_wrapped_e.value)
                for x in (
                    'No password available in keyring for ',
                    'No recommended backend was available.',
                )
            )
    elif reporter in ('pushover', 'pushbullet', 'telegram', 'matrix', 'mailgun', 'prowl'):
        if reporter == 'matrix' and not matrix_client_is_installed:
            logger.warning(f"Skipping {reporter} since 'matrix' package is not installed")
            return
        with pytest.raises(RuntimeError) as pytest_wrapped_e:
            report.finish_one(reporter, check_enabled=False)
        assert reporter in str(pytest_wrapped_e.value).lower()
    elif reporter in ('webhook', 'webhook_markdown'):
        with pytest.raises(MissingSchema) as pytest_wrapped_e:
            report.finish_one(reporter, check_enabled=False)
        assert str(pytest_wrapped_e.value) == "Invalid URL '': No schema supplied. Perhaps you meant http://?"
    elif reporter != 'browser' or 'PYCHARM_HOSTED' in os.environ:
        report.finish_one(reporter, check_enabled=False)
