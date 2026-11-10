import asyncio

from mcp_ibm_mq.audit import AuditLog
from mcp_ibm_mq.mq_client import InMemoryClient
from mcp_ibm_mq.policy import Policy
from mcp_ibm_mq.server import create_server
from mcp_ibm_mq.service import MQService


def test_tools_are_registered():
    svc = MQService(InMemoryClient(), Policy(), AuditLog(path="/dev/null"))
    server = create_server(svc)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {"list_queues", "queue_depth", "browse_messages", "put_message", "get_message"}
