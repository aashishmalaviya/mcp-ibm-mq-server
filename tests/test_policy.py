import pytest

from mcp_ibm_mq.policy import Policy, PolicyError


def test_defaults_are_read_only_and_deny_all():
    p = Policy.from_env({})
    assert p.read_only is True
    with pytest.raises(PolicyError):
        p.check_read("ANY.Q")
    with pytest.raises(PolicyError):
        p.check_write("ANY.Q", b"x")


def test_env_parsing():
    p = Policy.from_env({
        "MQ_READ_QUEUES": "ORDERS.STATUS, CLAIMS.STATUS",
        "MQ_WRITE_QUEUES": "ORDERS.REQUEST",
        "MQ_READ_ONLY": "false",
        "MQ_MAX_MESSAGE_BYTES": "10",
    })
    assert p.read_queues == {"ORDERS.STATUS", "CLAIMS.STATUS"}
    p.check_read("ORDERS.STATUS")
    p.check_write("ORDERS.REQUEST", b"hello")
    with pytest.raises(PolicyError, match="limit"):
        p.check_write("ORDERS.REQUEST", b"x" * 11)


def test_destructive_get_needs_two_switches():
    base = {"MQ_READ_QUEUES": "Q1"}
    with pytest.raises(PolicyError):
        Policy.from_env({**base, "MQ_READ_ONLY": "false"}).check_destructive_get("Q1")
    Policy.from_env(
        {**base, "MQ_READ_ONLY": "false", "MQ_ALLOW_DESTRUCTIVE_GET": "true"}
    ).check_destructive_get("Q1")


def test_browse_is_clamped():
    p = Policy(max_browse=3)
    assert p.clamp_browse(100) == 3
    assert p.clamp_browse(0) == 1
