import pytest

# Import ours first: HA mounts the plugin's testing_config dir and imports its
# custom_components package otherwise (homeassistant/loader.py _async_mount_config_dir).
import custom_components  # noqa: F401


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load integrations from custom_components/."""
    yield
