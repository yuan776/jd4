import cloudpickle
from asyncio import gather, get_event_loop, open_connection
from butter.clone import unshare, CLONE_NEWNS, CLONE_NEWUTS, CLONE_NEWIPC, CLONE_NEWUSER, CLONE_NEWPID, CLONE_NEWNET
from butter.system import mount, pivot_root, umount, MS_BIND, MS_NOSUID, MS_RDONLY, MS_REMOUNT
from os import chdir, fork, getegid, geteuid, listdir, makedirs, mkdir, mknod, path, \
    remove, readlink, rmdir, setresgid, setresuid, spawnve, symlink, waitpid, P_WAIT
from shutil import rmtree
from socket import sethostname, socketpair
from struct import pack, unpack
from sys import exit
from tempfile import mkdtemp

from jd4.log import logger
from jd4.util import write_text_file

MNT_DETACH = 2

def remove_under(*dirnames):
    for dirname in dirnames:
        for name in listdir(dirname):
            full_path = path.join(dirname, name)
            if path.isdir(full_path):
                rmtree(full_path)
            else:
                remove(full_path)

class Sandbox:
    def __init__(self, pid, sandbox_dir, in_dir, out_dir, reader, writer):
        self.pid = pid
        self.sandbox_dir = sandbox_dir
        self.in_dir = in_dir
        self.out_dir = out_dir
        self.reader = reader
        self.writer = writer

    def __del__(self):
        self.writer.write_eof()
        waitpid(self.pid, 0)
        rmtree(self.sandbox_dir)

    async def reset(self):
        loop = get_event_loop()
        await gather(loop.run_in_executor(None, remove_under, self.in_dir, self.out_dir),
                     self.marshal(lambda: remove_under('/tmp')))

    async def marshal(self, func):
        cloudpickle.dump(func, self.writer)
        length, = unpack('I', await self.reader.read(4))
        ret, err = cloudpickle.loads(await self.reader.read(length))
        if err:
            raise err
        return ret

    async def backdoor(self):
        return await self.marshal(lambda: spawnve(
            P_WAIT, '/bin/bash', ['bunny'], {'PATH': '/usr/bin:/bin', 'HOME': '/'}))

def _create_namespace():
    host_euid = geteuid()
    host_egid = getegid()
    unshare(CLONE_NEWNS | CLONE_NEWUTS | CLONE_NEWIPC |
            CLONE_NEWUSER | CLONE_NEWPID | CLONE_NEWNET)
    write_text_file('/proc/self/uid_map', '1000 {} 1'.format(host_euid))
    try:
        write_text_file('/proc/self/setgroups', 'deny')
    except FileNotFoundError:
        pass
    write_text_file('/proc/self/gid_map', '1000 {} 1'.format(host_egid))
    setresuid(1000, 1000, 1000)
    setresgid(1000, 1000, 1000)
    sethostname('icebox')

def bind_mount(src, target, *, make_dir=False, make_node=False, bind=False, rebind_ro=False):
    if make_dir:
        makedirs(target)
    if make_node:
        mknod(target)
    if bind:
        mount(src, target, '', MS_BIND | MS_NOSUID)
    if rebind_ro:
        mount(src, target, '', MS_BIND | MS_REMOUNT | MS_RDONLY | MS_NOSUID)

def bind_or_link(src, target):
    if path.islink(src):
        symlink(readlink(src), target)
    elif path.isdir(src):
        bind_mount(src, target, make_dir=True, bind=True, rebind_ro=True)

def _prepare_fs():
    mkdir('proc')
    mount('proc', 'proc', 'proc', MS_NOSUID)
    mkdir('dev')
    bind_mount('/dev/null', 'dev/null', make_node=True, bind=True)
    mkdir('tmp')
    mount('tmp', 'tmp', 'tmpfs', MS_NOSUID, "size=16m,nr_inodes=4k")
    bind_or_link('/bin', 'bin')
    bind_or_link('/etc/alternatives', 'etc/alternatives')
    bind_or_link('/lib', 'lib')
    bind_or_link('/lib64', 'lib64')
    bind_or_link('/usr/bin', 'usr/bin')
    bind_or_link('/usr/include', 'usr/include')
    bind_or_link('/usr/lib', 'usr/lib')
    bind_or_link('/usr/lib64', 'usr/lib64')
    bind_or_link('/usr/libexec', 'usr/libexec')
    bind_or_link('/var/lib/ghc', 'var/lib/ghc')
    write_text_file('etc/passwd', 'icebox:x:1000:1000:icebox:/:/bin/bash\n')

def _enter_namespace():
    mkdir('old_root')
    pivot_root('.', 'old_root')
    umount('old_root', MNT_DETACH)
    rmdir('old_root')
    bind_mount('/', '/', rebind_ro=True)

def _handle_child(child_socket, root_dir, in_dir, out_dir):
    _create_namespace()
    pid = fork()
    if pid != 0:
        child_socket.close()
        waitpid(pid, 0)
        exit()

    mount('root', root_dir, 'tmpfs', MS_NOSUID)
    chdir(root_dir)
    _prepare_fs()
    bind_mount(in_dir, 'in', make_dir=True, bind=True, rebind_ro=True)
    bind_mount(out_dir, 'out', make_dir=True, bind=True)
    _enter_namespace()

    socket_file = child_socket.makefile('rwb')
    while True:
        try:
            func = cloudpickle.load(socket_file)
        except EOFError:
            exit()
        try:
            ret, err = func(), None
        except Exception as e:
            ret, err = None, e
        data = cloudpickle.dumps((ret, err))
        socket_file.write(pack('I', len(data)))
        socket_file.write(data)
        socket_file.flush()

def create_sandboxes(n):
    parent_sockets = list()
    sandbox_params = list()
    for i in range(n):
        sandbox_dir = mkdtemp(prefix='jd4.sandbox.')
        root_dir = path.join(sandbox_dir, 'root')
        mkdir(root_dir)
        in_dir = path.join(sandbox_dir, 'in')
        mkdir(in_dir)
        out_dir = path.join(sandbox_dir, 'out')
        mkdir(out_dir)
        parent_socket, child_socket = socketpair()
        parent_sockets.append(parent_socket)

        pid = fork()
        if pid == 0:
            for parent_socket in parent_sockets:
                parent_socket.close()
            _handle_child(child_socket, root_dir, in_dir, out_dir)
        child_socket.close()
        sandbox_params.append((pid, sandbox_dir, in_dir, out_dir, parent_socket))

    async def helper(pid, sandbox_dir, in_dir, out_dir, parent_socket):
        reader, writer = await open_connection(sock=parent_socket)
        return Sandbox(pid, sandbox_dir, in_dir, out_dir, reader, writer)

    return gather(*[helper(*sp) for sp in sandbox_params])

async def create_sandbox():
    sandbox, = await create_sandboxes(1)
    return sandbox

if __name__ == '__main__':
    async def main():
        sandbox = await create_sandbox()
        logger.info('sandbox_dir: %s', sandbox.sandbox_dir)
        logger.info('return value: %d', await sandbox.backdoor())

    get_event_loop().run_until_complete(main())
