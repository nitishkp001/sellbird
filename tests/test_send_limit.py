"""The daily send limit: written into the config file in place, and picked up by a running Sellbird."""
import pytest

from sellbird.config import ConfigError, load_config, refresh_sending_mode, set_max_daily_sends

import shutil

from sellbird.paths import PROJECT_ROOT


@pytest.fixture
def yaml_file(tmp_path):
    path = tmp_path / 'sellbird.yaml'
    shutil.copy(PROJECT_ROOT / 'sellbird.yaml', path)
    return path


def test_only_the_limit_line_changes(yaml_file):
    before = yaml_file.read_text().splitlines()
    set_max_daily_sends(12, yaml_file)
    after = yaml_file.read_text().splitlines()
    assert len(before) == len(after) and len([1 for a, b in zip(before, after) if a != b]) == 1
    assert load_config(str(yaml_file)).channels.email.max_daily_sends == 12


def test_a_missing_line_is_added_under_email(yaml_file):
    import re
    yaml_file.write_text(re.sub(r'^[ \t]*max_daily_sends:.*\n', '', yaml_file.read_text(), flags=re.M))
    set_max_daily_sends(8, yaml_file)
    assert load_config(str(yaml_file)).channels.email.max_daily_sends == 8


@pytest.mark.parametrize('bad', [-1, 501, 2.5, '10', True, None])
def test_nonsense_limits_are_refused(yaml_file, bad):
    before = yaml_file.read_text()
    with pytest.raises(ConfigError):
        set_max_daily_sends(bad, yaml_file)
    assert yaml_file.read_text() == before


def test_a_running_config_follows_the_file(yaml_file):
    config = load_config(str(yaml_file))
    before = config.channels.email.max_daily_sends
    assert refresh_sending_mode(config, yaml_file) is False
    assert config.channels.email.max_daily_sends == before
    set_max_daily_sends(before + 7, yaml_file)
    assert refresh_sending_mode(config, yaml_file) is False      # the mode didn't change
    assert config.channels.email.max_daily_sends == before + 7


@pytest.mark.parametrize('bad', ['true', 'lots', '-3'])
def test_a_limit_that_doesnt_read_keeps_the_running_one(yaml_file, bad):
    import re
    config = load_config(str(yaml_file))
    before = config.channels.email.max_daily_sends
    yaml_file.write_text(re.sub(r'max_daily_sends:.*', f'max_daily_sends: {bad}', yaml_file.read_text(), count=1))
    refresh_sending_mode(config, yaml_file)
    assert config.channels.email.max_daily_sends == before
