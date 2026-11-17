"""Verify the offline boundary itself, including Windows' asynchronous TCP path."""

import asyncio
import socket
import subprocess

import httpx
import pytest


@pytest.mark.anyio
async def test_unmocked_network_and_processes_are_refused():
    message = "cannot open network connections or subprocesses"
    with socket.socket() as connection, pytest.raises(AssertionError, match=message):
        connection.connect(("127.0.0.1", 10100))
    with pytest.raises(AssertionError, match=message):
        await asyncio.get_running_loop().create_connection(asyncio.Protocol, "127.0.0.1", 10100)
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(AssertionError, match=message):
            await client.get("https://unmocked.invalid")
    with pytest.raises(AssertionError, match=message):
        subprocess.Popen(["must-not-start"])
