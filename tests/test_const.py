from custom_components.druckwerk.const import FORWARD_TIMEOUT


def test_forward_timeout_fits_the_longest_app_call():
    # The longest app call is a start (400 s). A longer wait only holds a worker of Home Assistant.
    assert 400 < FORWARD_TIMEOUT <= 500
