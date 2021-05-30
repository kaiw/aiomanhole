import asyncio
import contextlib
import functools
import sys
import traceback
from types import CodeType
from typing import Any, Awaitable, Dict, List, Optional, Tuple, Type, Union

from codeop import CommandCompiler
from io import BytesIO, StringIO


__all__ = ['start_manhole']

Namespace = Dict[str, Any]


class StatefulCommandCompiler(CommandCompiler):
    """A command compiler that buffers input until a full command is available."""

    def __init__(self) -> None:
        super().__init__()
        self.buf = BytesIO()

    def is_partial_command(self) -> bool:
        return bool(self.buf.getvalue())

    def __call__(self, source: bytes, **kwargs: Any) -> Optional[CodeType]:
        buf = self.buf
        if self.is_partial_command():
            buf.write(b'\n')
        buf.write(source)

        code = self.buf.getvalue().decode('utf8')

        codeobj = super().__call__(code, **kwargs)

        if codeobj:
            self.reset()
        return codeobj

    def reset(self) -> None:
        self.buf.seek(0)
        self.buf.truncate(0)


class InteractiveInterpreter:
    """An interactive asynchronous interpreter."""

    def __init__(
        self,
        namespace: Optional[Namespace],
        banner: Union[None, bytes, str],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.namespace = namespace
        self.banner = self.get_banner(banner)
        self.compiler = StatefulCommandCompiler()
        self.loop = loop

    def get_banner(self, banner: Union[None, bytes, str]) -> bytes:
        if isinstance(banner, bytes):
            return banner
        elif isinstance(banner, str):
            return banner.encode('utf8')
        elif banner is None:
            return b''
        else:
            raise ValueError(
                "Cannot handle unknown banner type {!r}, expected str or bytes".format(
                    banner.__class__.__name__
                )
            )

    def attempt_compile(self, line: bytes) -> Optional[CodeType]:
        return self.compiler(line)

    async def send_exception(self) -> None:
        """When an exception has occurred, write the traceback to the user."""
        self.compiler.reset()

        exc = traceback.format_exc()
        self.writer.write(exc.encode('utf8'))

        await self.writer.drain()

    async def attempt_exec(
        self, codeobj: CodeType, namespace: Optional[Namespace]
    ) -> Tuple[Any, str]:
        with contextlib.redirect_stdout(StringIO()) as buf:
            value = await self._real_exec(codeobj, namespace)

        return value, buf.getvalue()

    async def _real_exec(
        self, codeobj: CodeType, namespace: Optional[Namespace]
    ) -> Any:
        return eval(codeobj, namespace)

    async def handle_one_command(self) -> None:
        """Process a single command. May have many lines."""

        while True:
            await self.write_prompt()
            codeobj = await self.read_command()

            if codeobj is not None:
                await self.run_command(codeobj)

    async def run_command(self, codeobj: CodeType) -> None:
        """Execute a compiled code object, and write the output back to the client."""
        try:
            value, stdout = await self.attempt_exec(codeobj, self.namespace)
        except Exception:
            await self.send_exception()
            return
        else:
            await self.send_output(value, stdout)

    async def write_prompt(self) -> None:
        writer = self.writer

        if self.compiler.is_partial_command():
            writer.write(sys.ps2.encode('utf8'))
        else:
            writer.write(sys.ps1.encode('utf8'))

        await writer.drain()

    async def read_command(self) -> Optional[CodeType]:
        """Read a command from the user line by line.

        Returns a code object suitable for execution.
        """

        reader = self.reader

        line = await reader.readline()
        if line == b'':  # lost connection
            raise ConnectionResetError()

        try:
            # skip the newline to make CommandCompiler work as advertised
            codeobj = self.attempt_compile(line.rstrip(b'\n'))
        except SyntaxError:
            await self.send_exception()
            return None

        return codeobj

    async def send_output(self, value: Any, stdout: str) -> None:
        """Write the output or value of the expression back to user.

        >>> 5
        5
        >>> print('cash rules everything around me')
        cash rules everything around me
        """

        writer = self.writer

        if value is not None:
            writer.write('{!r}\n'.format(value).encode('utf8'))

        if stdout:
            writer.write(stdout.encode('utf8'))

        await writer.drain()

    def _setup_prompts(self) -> None:
        try:
            sys.ps1
        except AttributeError:
            sys.ps1 = ">>> "
        try:
            sys.ps2
        except AttributeError:
            sys.ps2 = "... "

    async def __call__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Main entry point for an interpreter session with a single client."""

        self.reader = reader
        self.writer = writer

        self._setup_prompts()

        if self.banner:
            writer.write(self.banner)
            await writer.drain()

        while True:
            try:
                await self.handle_one_command()
            except ConnectionResetError:
                writer.close()
                break
            except Exception:
                traceback.print_exc()


class ThreadedInteractiveInterpreter(InteractiveInterpreter):
    """An interactive asynchronous interpreter that executes
    statements/expressions in a thread.

    This is useful for aiding to protect against accidentally running
    slow/terminal code in your main loop, which would destroy the process.

    Also accepts a timeout, which defaults to five seconds. This won't kill
    the running statement (good luck killing a thread) but it will at least
    yield control back to the manhole.
    """

    def __init__(self, *args: Any, command_timeout: int = 5, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.command_timeout = command_timeout

    async def _real_exec(
        self, codeobj: CodeType, namespace: Optional[Namespace]
    ) -> Any:
        task = self.loop.run_in_executor(None, eval, codeobj, namespace)
        if self.command_timeout:
            task = asyncio.wait_for(task, self.command_timeout, loop=self.loop)
        return await task


class InterpreterFactory:
    """Factory class for creating interpreters."""

    def __init__(
        self,
        interpreter_class: Type[InteractiveInterpreter],
        *args: Any,
        namespace: Optional[Namespace] = None,
        shared: bool = False,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        **kwargs: Any,
    ) -> None:
        self.interpreter_class = interpreter_class
        self.namespace = namespace or {}
        self.shared = shared
        self.args = args
        self.kwargs = kwargs
        self.loop = loop or asyncio.get_event_loop()

    def __call__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> Awaitable[None]:
        interpreter = self.interpreter_class(
            *self.args,
            loop=self.loop,
            namespace=self.namespace if self.shared else dict(self.namespace),
            **self.kwargs,
        )
        return asyncio.ensure_future(interpreter(reader, writer), loop=self.loop)


def start_manhole(
    banner: Union[None, bytes, str] = None,
    host: str = '127.0.0.1',
    port: Optional[int] = None,
    path: Optional[str] = None,
    namespace: Optional[Namespace] = None,
    loop: Optional[asyncio.AbstractEventLoop] = None,
    threaded: bool = False,
    command_timeout: int = 5,
    shared: bool = False,
) -> 'asyncio.Future[Tuple[asyncio.AbstractServer, ...]]':

    """Starts a manhole server on a given TCP and/or UNIX address.

    Keyword arguments:
        banner - Text to display when client initially connects.
        host - interface to bind on.
        port - port to listen on over TCP. Default is disabled.
        path - filesystem path to listen on over UNIX sockets. Deafult is disabled.
        namespace - dictionary namespace to provide to connected clients.
        threaded - if True, use a threaded interpreter. False, run them in the
                   middle of the event loop. See ThreadedInteractiveInterpreter
                   for details.
        command_timeout - timeout in seconds for commands. Only applies if
                          `threaded` is True.
        shared - If True, share a single namespace between all clients.

    Returns a Future for starting the server(s).
    """

    loop = loop or asyncio.get_event_loop()

    if (port, path) == (None, None):
        raise ValueError('At least one of port or path must be given')

    if threaded:
        interpreter_class = functools.partial(
            ThreadedInteractiveInterpreter, command_timeout=command_timeout
        )
    else:
        interpreter_class = InteractiveInterpreter

    client_cb = InterpreterFactory(
        interpreter_class, shared=shared, namespace=namespace, banner=banner, loop=loop
    )

    coros: List[asyncio.Future[asyncio.AbstractServer]] = []

    if path:
        f = asyncio.ensure_future(
            asyncio.start_unix_server(client_cb, path=path, loop=loop), loop=loop
        )
        coros.append(f)

    if port is not None:
        f = asyncio.ensure_future(
            asyncio.start_server(client_cb, host=host, port=port, loop=loop), loop=loop
        )
        coros.append(f)

    return asyncio.gather(*coros, loop=loop)


if __name__ == '__main__':
    start_manhole(
        path='/var/tmp/testing.manhole',
        banner='Well this is neat\n',
        threaded=True,
        shared=True,
    )
    asyncio.get_event_loop().run_forever()
