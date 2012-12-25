import errno
import os
import stat
import time

from beaver.utils import REOPEN_FILES, eglob


class Worker(object):
    """Looks for changes in all files of a directory.
    This is useful for watching log file changes in real-time.
    It also supports files rotation.

    Example:

    >>> def callback(filename, lines):
    ...     print filename, lines
    ...
    >>> l = Worker(args, callback, ["log", "txt"], tail_lines=0)
    >>> l.loop()
    """

    def __init__(self, beaver_config, file_config, callback, logger=None, extensions=["log"], tail_lines=0):
        """Arguments:

        (FileConfig) @file_config:
            object containing file-related configuration

        (BeaverConfig) @beaver_config:
            object containing global configuration

        (Logger) @logger
            object containing a python logger

        (callable) @callback:
            a function which is called every time a new line in a
            file being watched is found;
            this is called with "filename" and "lines" arguments.

        (list) @extensions:
            only watch files with these extensions

        (int) @tail_lines:
            read last N lines from files being watched before starting
        """
        self.beaver_config = beaver_config
        self.file_config = file_config
        self.callback = callback
        self.extensions = extensions
        self.files_map = {}
        self._logger = logger

        if self.beaver_config.get('path') is not None:
            self.folder = os.path.realpath(self.beaver_config.get('path'))
            assert os.path.isdir(self.folder), "%s does not exists" \
                                            % self.folder
        assert callable(callback)
        self.update_files()
        # The first time we run the script we move all file markers at EOF.
        # In case of files created afterwards we don't do this.
        for id, file in self.files_map.iteritems():
            file.seek(os.path.getsize(file.name))  # EOF
            if tail_lines:
                lines = self.tail(file.name, tail_lines)
                if lines:
                    self.callback(file.name, lines)

    def __del__(self):
        """Closes all files"""
        self.close()

    def close(self):
        """Closes all currently open file pointers"""
        for id, file in self.files_map.iteritems():
            file.close()
        self.files_map.clear()

    def listdir(self):
        """List directory and filter files by extension.
        You may want to override this to add extra logic or
        globbling support.
        """
        ls = os.listdir(self.folder)
        if self.extensions:
            return [x for x in ls if os.path.splitext(x)[1][1:] \
                                           in self.extensions]
        else:
            return ls

    def loop(self, interval=0.1, async=False):
        """Start the loop.
        If async is True make one loop then return.
        """
        while 1:
            self.update_files()
            for fid, file in list(self.files_map.iteritems()):
                try:
                    self.readfile(fid, file)
                except IOError, e:
                    if e.errno == errno.ESTALE:
                        self.unwatch(file, fid)
            if async:
                return
            time.sleep(interval)

    def readfile(self, fid, file):
        """Read lines from a file and performs a callback against them"""
        lines = file.readlines()
        if lines:
            self.callback(file.name, lines)

    def update_files(self):
        """Ensures all files are properly loaded.
        Detects new files, file removals, file rotation, and truncation.
        On non-linux platforms, it will also manually reload the file for tailing.
        Note that this hack is necessary because EOF is cached on BSD systems.
        """
        ls = []
        files = []
        if len(self.beaver_config.get('globs')) > 0:
            for name in self.beaver_config.get('globs'):
                globbed = [os.path.realpath(filename) for filename in eglob(name)]
                files.extend(globbed)
                self.file_config.addglob(name, globbed)
        else:
            for name in self.listdir():
                files.append(os.path.realpath(os.path.join(self.folder, name)))

        for absname in files:
            try:
                st = os.stat(absname)
            except EnvironmentError, err:
                if err.errno != errno.ENOENT:
                    raise
            else:
                if not stat.S_ISREG(st.st_mode):
                    continue
                fid = self.get_file_id(st)
                ls.append((fid, absname))

        # check existent files
        for fid, file in list(self.files_map.iteritems()):
            try:
                st = os.stat(file.name)
            except EnvironmentError, err:
                if err.errno == errno.ENOENT:
                    self.unwatch(file, fid)
                else:
                    raise
            else:
                if fid != self.get_file_id(st):
                    self._logger.info("[{0}] - file rotated {1}".format(fid, file.name))
                    self.unwatch(file, fid)
                    self.watch(file.name)
                elif file.tell() > st.st_size:
                    self._logger.info("[{0}] - file truncated {1}".format(fid, file.name))
                    self.unwatch(file, fid)
                    self.watch(file.name)
                elif REOPEN_FILES:
                    self._logger.debug("[{0}] - file reloaded (non-linux) {1}".format(fid, file.name))
                    position = file.tell()
                    fname = file.name
                    file.close()
                    file = open(fname, "r")
                    file.seek(position)
                    self.files_map[fid] = file

        # add new ones
        for fid, fname in ls:
            if fid not in self.files_map:
                self.watch(fname)

    def unwatch(self, file, fid):
        """file no longer exists; if it has been renamed
        try to read it for the last time in case the
        log rotator has written something in it.
        """
        try:
            self.readfile(fid, file)
        except IOError:
            # Silently ignore any IOErrors -- file is gone
            pass
        self._logger.info("[{0}] - un-watching logfile {1}".format(fid, file.name))
        del self.files_map[fid]

    def watch(self, fname):
        """Opens a file for log tailing"""
        try:
            file = open(fname, "r")
            fid = self.get_file_id(os.stat(fname))
        except EnvironmentError, err:
            if err.errno != errno.ENOENT:
                raise
        else:
            self._logger.info("[{0}] - watching logfile {1}".format(fid, fname))
            self.files_map[fid] = file

    @staticmethod
    def get_file_id(st):
        return "%xg%x" % (st.st_dev, st.st_ino)

    @staticmethod
    def tail(fname, window):
        """Read last N lines from file fname."""
        try:
            f = open(fname, 'r')
        except IOError, err:
            if err.errno == errno.ENOENT:
                return []
            else:
                raise
        else:
            BUFSIZ = 1024
            f.seek(0, os.SEEK_END)
            fsize = f.tell()
            block = -1
            data = ""
            exit = False
            while not exit:
                step = (block * BUFSIZ)
                if abs(step) >= fsize:
                    f.seek(0)
                    exit = True
                else:
                    f.seek(step, os.SEEK_END)
                data = f.read().strip()
                if data.count('\n') >= window:
                    break
                else:
                    block -= 1
            return data.splitlines()[-window:]
