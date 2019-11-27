#!/usr/bin/python -u
#
# p7zr library
#
# Copyright (c) 2019 Hiroshi Miura <miurahr@linux.com>
# Copyright (c) 2004-2015 by Joachim Bauch, mail@joachim-bauch.de
# 7-Zip Copyright (C) 1999-2010 Igor Pavlov
# LZMA SDK Copyright (C) 1999-2010 Igor Pavlov
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
#
#
"""Read 7zip format archives."""
import datetime
import errno
import functools
import io
import operator
import os
import stat
import sys
import threading
from io import BytesIO
from typing import Any, BinaryIO, Dict, List, Optional, Tuple, Union

from py7zr.archiveinfo import Folder, Header, SignatureHeader
from py7zr.compression import SevenZipCompressor, Worker, get_methods_names
from py7zr.exceptions import Bad7zFile
from py7zr.helpers import ArchiveTimestamp, calculate_crc32, filetime_to_dt
from py7zr.properties import MAGIC_7Z, Configuration, FileAttribute

if sys.version_info < (3, 6):
    import pathlib2 as pathlib
else:
    import pathlib


class ArchiveFile:
    """Represent each files metadata inside archive file.
    It holds file properties; filename, permissions, and type whether
    it is directory, link or normal file.

    Instances of the :class:`ArchiveFile` class are returned by iterating :attr:`files_list` of
     :class:`SevenZipFile` objects.
    Each object stores information about a single member of the 7z archive. Most of users use :meth:`extractall()`.

    The class also hold an archive parameter where file is exist in
    archive file folder(container)."""
    def __init__(self, id: int, file_info: Dict[str, Any]) -> None:
        self.id = id
        self._file_info = file_info

    def file_properties(self) -> Dict[str, Any]:
        """Return file properties as a hash object. Following keys are included: ‘readonly’, ‘is_directory’,
         ‘posix_mode’, ‘archivable’, ‘emptystream’, ‘filename’, ‘creationtime’, ‘lastaccesstime’,
          ‘lastwritetime’, ‘attributes’
        """
        properties = self._file_info
        if properties is not None:
            properties['readonly'] = self.readonly
            properties['posix_mode'] = self.posix_mode
            properties['archivable'] = self.archivable
            properties['is_directory'] = self.is_directory
        return properties

    def _get_property(self, key: str) -> Any:
        try:
            return self._file_info[key]
        except KeyError:
            return None

    @property
    def origin(self) -> str:
        return self._get_property('origin')

    @property
    def folder(self) -> Folder:
        return self._get_property('folder')

    @property
    def filename(self) -> str:
        """return filename of archive file."""
        return self._get_property('filename')

    @property
    def emptystream(self) -> bool:
        """True if file is empty(0-byte file), otherwise False"""
        return self._get_property('emptystream')

    @property
    def uncompressed(self) -> List[int]:
        return self._get_property('uncompressed')

    @property
    def uncompressed_size(self) -> int:
        """Uncompressed file size."""
        return functools.reduce(operator.add, self.uncompressed)

    @property
    def compressed(self) -> Optional[int]:
        """Compressed size"""
        return self._get_property('compressed')

    def _test_attribute(self, target_bit: FileAttribute) -> bool:
        attributes = self._get_property('attributes')
        if attributes is None:
            return False
        return attributes & target_bit == target_bit

    @property
    def archivable(self) -> bool:
        """File has a Windows `archive` flag."""
        return self._test_attribute(FileAttribute.ARCHIVE)

    @property
    def is_directory(self) -> bool:
        """True if file is a directory, otherwise False."""
        return self._test_attribute(FileAttribute.DIRECTORY)

    @property
    def readonly(self) -> bool:
        """True if file is readonly, otherwise False."""
        return self._test_attribute(FileAttribute.READONLY)

    def _get_unix_extension(self) -> Optional[int]:
        attributes = self._get_property('attributes')
        if self._test_attribute(FileAttribute.UNIX_EXTENSION):
            return attributes >> 16
        return None

    @property
    def is_symlink(self) -> bool:
        """True if file is a symbolic link, otherwise False."""
        e = self._get_unix_extension()
        if e is not None:
            return stat.S_ISLNK(e)
        return False

    @property
    def is_socket(self) -> bool:
        """True if file is a socket, otherwise False."""
        e = self._get_unix_extension()
        if e is not None:
            return stat.S_ISSOCK(e)
        return False

    @property
    def lastwritetime(self) -> Optional[ArchiveTimestamp]:
        """Return last written timestamp of a file."""
        return self._get_property('lastwritetime')

    @property
    def posix_mode(self) -> Optional[int]:
        """
        posix mode when a member has a unix extension property, or None
        :return: Return file stat mode can be set by os.chmod()
        """
        e = self._get_unix_extension()
        if e is not None:
            return stat.S_IMODE(e)
        return None

    @property
    def st_fmt(self) -> Optional[int]:
        """
        :return: Return the portion of the file mode that describes the file type
        """
        e = self._get_unix_extension()
        if e is not None:
            return stat.S_IFMT(e)
        return None


class ArchiveFileList:
    """Iteratable container of ArchiveFile."""

    def __init__(self, offset: int = 0):
        self.files_list = []  # type: List[dict]
        self.index = 0
        self.offset = offset

    def append(self, file_info: Dict[str, Any]) -> None:
        self.files_list.append(file_info)

    def __len__(self) -> int:
        return len(self.files_list)

    def __iter__(self) -> 'ArchiveFileList':
        self.index = 0
        return self

    def __next__(self) -> ArchiveFile:
        if self.index == len(self.files_list):
            raise StopIteration
        res = ArchiveFile(self.index + self.offset, self.files_list[self.index])
        self.index += 1
        return res


# ------------------
# Exported Classes
# ------------------
class ArchiveInfo:
    """Hold archive information"""

    def __init__(self, filename, size, header_size, method_names, solid, blocks):
        self.filename = filename
        self.size = size
        self.header_size = header_size
        self.method_names = method_names
        self.solid = solid
        self.blocks = blocks


class FileInfo:
    """Hold archived file information."""

    def __init__(self, filename, compressed, uncompressed, archivable, is_directory, creationtime):
        self.filename = filename
        self.compressed = compressed
        self.uncompressed = uncompressed
        self.archivable = archivable
        self.is_directory = is_directory
        self.creationtime = creationtime


class SevenZipFile:
    """The SevenZipFile Class provides an interface to 7z archives."""

    def __init__(self, file: Union[BinaryIO, str, pathlib.Path], mode: str = 'r', filters: Optional[str] = None) -> None:
        if mode not in ('r', 'w', 'x', 'a'):
            raise ValueError("ZipFile requires mode 'r', 'w', 'x', or 'a'")
        # Check if we were passed a file-like object or not
        if isinstance(file, str):
            self._filePassed = False  # type: bool
            self.filename = file  # type: str
            if mode == 'r':
                self.fp = open(file, 'rb')  # type: BinaryIO
            elif mode == 'w':
                self.fp = open(file, 'w+b')
            elif mode == 'x':
                self.fp = open(file, 'x+b')
            elif mode == 'a':
                self.fp = open(file, 'r+b')
            else:
                raise ValueError("File open error.")
            self.mode = mode
        elif isinstance(file, pathlib.Path):
            self._filePassed = False
            self.filename = str(file)
            if mode == 'r':
                self.fp = file.open(mode='rb')  # type: ignore  # noqa   # typeshed issue: 2911
            elif mode == 'w':
                self.fp = file.open(mode='w+b')  # type: ignore  # noqa
            elif mode == 'x':
                self.fp = file.open(mode='x+b')  # type: ignore  # noqa
            elif mode == 'a':
                self.fp = file.open(mode='r+b')  # type: ignore  # noqa
            else:
                raise ValueError("File open error.")
            self.mode = mode
        elif isinstance(file, io.IOBase):
            self._filePassed = True
            self.fp = file
            self.filename = getattr(file, 'name', None)
            self.mode = mode  # type: ignore  #noqa
        else:
            raise TypeError("invalid file: {}".format(type(file)))
        self._fileRefCnt = 1
        self._lock = threading.RLock()
        self.solid = False
        try:
            if mode == "r":
                self._real_get_contents(self.fp)
                self.reset()
            elif mode in 'w':
                self._write_open()
            elif mode in 'x':
                raise NotImplementedError
            elif mode == 'a':
                raise NotImplementedError
            else:
                raise ValueError("Mode must be 'r', 'w', 'x', or 'a'")
        except Exception as e:
            self._fpclose()
            raise e

    def _write_open(self):
        self.folder = self._create_folder()
        self.files = ArchiveFileList()

    def _create_folder(self):
        folder = Folder()
        folder.compressor = SevenZipCompressor()
        folder.coders = folder.compressor.coders
        folder.solid = True
        folder.digestdefined = False
        folder.bindpairs = []
        folder.totalin = 1
        folder.totalout = 1
        return folder

    def _fpclose(self) -> None:
        assert self._fileRefCnt > 0
        self._fileRefCnt -= 1
        if not self._fileRefCnt and not self._filePassed:
            self.fp.close()

    def _real_get_contents(self, fp: BinaryIO) -> None:
        if not self._check_7zfile(fp):
            raise Bad7zFile('not a 7z file')
        self.sig_header = SignatureHeader.retrieve(self.fp)
        self.afterheader = self.fp.tell()
        buffer = self._read_header_data()
        header = Header.retrieve(self.fp, buffer, self.afterheader)
        if header is None:
            return
        self.header = header
        buffer.close()
        self.solid = False
        self.files = ArchiveFileList()
        if getattr(self.header, 'files_info', None) is not None:
            self._filelist_retrieve()

    def _read_header_data(self) -> BytesIO:
        self.fp.seek(self.sig_header.nextheaderofs, os.SEEK_CUR)
        buffer = io.BytesIO(self.fp.read(self.sig_header.nextheadersize))
        if self.sig_header.nextheadercrc != calculate_crc32(buffer.getvalue()):
            raise Bad7zFile('invalid header data')
        return buffer

    def _filelist_retrieve(self) -> None:
        src_pos = self.afterheader
        # Initialize references for convenience
        if hasattr(self.header, 'main_streams'):
            folders = self.header.main_streams.unpackinfo.folders
            packinfo = self.header.main_streams.packinfo
            subinfo = self.header.main_streams.substreamsinfo
            packsizes = packinfo.packsizes
            self.solid = packinfo.numstreams == 1
            if subinfo.unpacksizes is not None:
                unpacksizes = subinfo.unpacksizes
            else:
                unpacksizes = [x.unpacksizes for x in folders]
        else:
            subinfo = None
            folders = None
            packinfo = None
            packsizes = []
            unpacksizes = [0]

        # Initialize loop index variables
        folder_index = 0
        output_binary_index = 0
        streamidx = 0
        file_in_solid = 0
        instreamindex = 0
        folder_pos = src_pos

        for file_id, file_info in enumerate(self.header.files_info.files):

            if not file_info['emptystream'] and folders is not None:
                folder = folders[folder_index]
                if streamidx == 0:
                    folder.solid = subinfo.num_unpackstreams_folders[folder_index] > 1

                file_info['maxsize'] = (folder.solid and packinfo.packsizes[instreamindex]) or None
                uncompressed = unpacksizes[output_binary_index]
                if not isinstance(uncompressed, (list, tuple)):
                    uncompressed = [uncompressed] * len(folder.coders)
                if file_in_solid > 0:
                    # file is part of solid archive
                    # assert instreamindex < len(packsizes), 'Folder outside index for solid archive'
                    # file_info['compressed'] = packsizes[instreamindex]
                    file_info['compressed'] = None
                elif instreamindex < len(packsizes):
                    # file is compressed
                    file_info['compressed'] = packsizes[instreamindex]
                else:
                    # file is not compressed
                    file_info['compressed'] = uncompressed
                file_info['uncompressed'] = uncompressed
                numinstreams = 1
                for coder in folder.coders:
                    numinstreams = max(numinstreams, coder.get('numinstreams', 1))
                file_info['packsizes'] = packsizes[instreamindex:instreamindex + numinstreams]
                streamidx += 1
            else:
                file_info['compressed'] = 0
                file_info['uncompressed'] = [0]
                file_info['packsizes'] = [0]
                folder = None
                file_info['maxsize'] = 0
                numinstreams = 1

            file_info['folder'] = folder
            if folder is not None and subinfo.digestsdefined[output_binary_index]:
                file_info['digest'] = subinfo.digests[output_binary_index]

            if 'filename' not in file_info:
                # compressed file is stored without a name, generate one
                try:
                    basefilename = self.filename
                except AttributeError:
                    # 7z archive file doesn't have a name
                    file_info['filename'] = 'contents'
                else:
                    if basefilename is not None:
                        fn, ext = os.path.splitext(os.path.basename(basefilename))
                        file_info['filename'] = fn
                    else:
                        file_info['filename'] = 'contents'

            self.files.append(file_info)

            if folder is not None:
                if folder.solid:
                    # file_in_solid += unpacksizes[output_binary_index]
                    file_in_solid = 1
                output_binary_index += 1
            else:
                src_pos += file_info['compressed']
            if folder is not None:
                if folder.files is None:
                    folder.files = ArchiveFileList(offset=file_id)
                folder.files.append(file_info)
            if folder is not None and streamidx >= subinfo.num_unpackstreams_folders[folder_index]:
                file_in_solid = 0
                for x in range(numinstreams):
                    folder_pos += packinfo.packsizes[instreamindex + x]
                src_pos = folder_pos
                folder_index += 1
                instreamindex += numinstreams
                streamidx = 0

    def _num_files(self) -> int:
        if getattr(self.header, 'files_info', None) is not None:
            return len(self.header.files_info.files)
        return 0

    def _set_file_property(self, outfilename: pathlib.Path, properties: Dict[str, Any]) -> None:
        # creation time
        creationtime = ArchiveTimestamp(properties['lastwritetime']).totimestamp()
        if creationtime is not None:
            os.utime(str(outfilename), times=(creationtime, creationtime))
        if os.name == 'posix':
            st_mode = properties['posix_mode']
            if st_mode is not None:
                outfilename.chmod(st_mode)
                return
        # fallback: only set readonly if specified
        if properties['readonly'] and not properties['is_directory']:
            ro_mask = 0o777 ^ (stat.S_IWRITE | stat.S_IWGRP | stat.S_IWOTH)
            outfilename.chmod(outfilename.stat().st_mode & ro_mask)

    def reset(self) -> None:
        """Seek to where archive data start in archive and recreate new worker."""
        self.fp.seek(self.afterheader)
        self.worker = Worker(self.files, self.afterheader, self.header)

    @staticmethod
    def _check_7zfile(fp: Union[BinaryIO, io.BufferedReader]) -> bool:
        result = MAGIC_7Z == fp.read(len(MAGIC_7Z))[:len(MAGIC_7Z)]
        fp.seek(-len(MAGIC_7Z), 1)
        return result

    def _get_method_names(self) -> str:
        methods_names = []  # type: List[str]
        for folder in self.header.main_streams.unpackinfo.folders:
            methods_names += get_methods_names(folder.coders)
        return ', '.join(x for x in methods_names)

    def _test_digest_raw(self, pos: int, size: int, crc: int) -> bool:
        self.fp.seek(pos)
        remaining_size = size
        digest = None
        while remaining_size > 0:
            block = min(Configuration.read_blocksize, remaining_size)
            digest = calculate_crc32(self.fp.read(block), digest)
            remaining_size -= block
        return digest == crc

    def _test_pack_digest(self) -> bool:
        self.reset()
        crcs = self.header.main_streams.packinfo.crcs
        if crcs is not None and len(crcs) > 0:
            # check packed stream's crc
            for i, p in enumerate(self.header.main_streams.packinfo.packpositions):
                if not self._test_digest_raw(p, self.header.main_streams.packinfo.packsizes[i], crcs[i]):
                    return False
        return True

    def _test_unpack_digest(self) -> bool:
        self.reset()
        for f in self.files:
            self.worker.register_filelike(f.id, None)
        try:
            self.worker.extract(self.fp)  # TODO: print progress
        except Bad7zFile:
            return False
        else:
            return True

    def _test_digests(self) -> bool:
        if self._test_pack_digest():
            if self._test_unpack_digest():
                return True
        return False

    def _write_archive(self):
        self.sig_header = SignatureHeader()
        self.sig_header._write_skelton(self.fp)
        self.afterheader = self.fp.tell()
        self.header = Header()
        self.folder.totalin = 1
        self.folder.totalout = 1
        self.folder.bindpairs = []
        self.folder.unpacksizes = []
        self.header.build_header([self.folder])

        compressor = self.folder.get_compressor()

        for f in self.files:
            file_info = {'filename': f.filename, 'emptystream': f.emptystream}
            self.header.files_info.files.append(file_info)
            if f.emptystream:
                pass
            else:
                self.header.main_streams.packinfo.numstreams += 1
                outsize = 0
                insize = 0
                with pathlib.Path(f.origin).open(mode='rb') as fd:
                    data = fd.read(Configuration.read_blocksize)
                    insize += len(data)
                    while data:
                        out = compressor.compress(data)
                        outsize += len(out)
                        self.fp.write(out)
                        data = fd.read(Configuration.read_blocksize)
                        insize += len(data)
                    out = compressor.flush()
                    outsize += len(out)
                    self.fp.write(out)
                self.header.main_streams.packinfo.packsizes.append(outsize)
                self.folder.unpacksizes.append(insize)
        pos = self.fp.tell()
        self.sig_header.nextheaderofs = pos - self.afterheader
        self.header.write(self.fp, encoded=False)
        buf = io.BytesIO()
        self.header.write(buf, encoded=False)
        data = buf.getvalue()
        self.sig_header.calccrc(data)
        self.sig_header.write(self.fp)
        return

    @staticmethod
    def _make_file_info(target: pathlib.Path, arcname: Optional[str] = None) -> Dict[str, Any]:
        f = {}  # type: Dict[str, Any]
        f['origin'] = target
        if arcname is not None:
            f['filename'] = arcname
        else:
            f['filename'] = str(target)
        if target.is_dir():
            f['emptystream'] = True
            f['attributes'] = FileAttribute.DIRECTORY
            if os.name == 'posix':
                f['attributes'] |= FileAttribute.UNIX_EXTENSION | (stat.S_IFDIR << 16)
        elif target.is_symlink():
            f['emptystream'] = True
            if os.name == 'posix':
                f['attributes'] = FileAttribute.UNIX_EXTENSION | (stat.S_IFLNK << 16)
            else:
                f['attributes'] = 0x0  # FIXME
        elif target.is_file():
            f['emptystream'] = False
            f['attributes'] = 0x0
            fstat = target.stat()
            f['uncompressed'] = fstat.st_size
        return f

    # --------------------------------------------------------------------------
    # The public methods which SevenZipFile provides:
    def getnames(self) -> List[str]:
        """Return the members of the archive as a list of their names. It has
           the same order as the list returned by getmembers().
        """
        return list(map(lambda x: x.filename, self.files))

    def archiveinfo(self) -> ArchiveInfo:
        fstat = os.stat(self.filename)
        return ArchiveInfo(self.filename, fstat.st_size, self.header.size, self._get_method_names(),
                           self.solid, len(self.header.main_streams.unpackinfo.folders))

    def list(self) -> List[FileInfo]:
        """Returns contents information """
        alist = []  # type: List[FileInfo]
        creationtime = None  # type: Optional[datetime.datetime]
        for f in self.files:
            if f.lastwritetime is not None:
                creationtime = filetime_to_dt(f.lastwritetime)
            alist.append(FileInfo(f.filename, f.compressed, f.uncompressed_size, f.archivable, f.is_directory,
                                  creationtime))
        return alist

    def test(self) -> bool:
        """Test archive using CRC digests."""
        return self._test_digests()

    def extractall(self, path: Optional[Any] = None) -> None:
        """Extract all members from the archive to the current working
           directory and set owner, modification time and permissions on
           directories afterwards. `path' specifies a different directory
           to extract to.
        """
        target_sym = []  # type: List[Tuple[BinaryIO, str]]
        target_files = []  # type: List[Tuple[pathlib.Path, Dict[str, Any]]]
        target_dirs = []  # type: List[pathlib.Path]
        self.reset()
        if path is not None:
            if isinstance(path, str):
                path = pathlib.Path(path)
            try:
                if not path.exists():
                    path.mkdir(parents=True)
                else:
                    pass
            except OSError as e:
                if e.errno == errno.EEXIST and path.is_dir():
                    pass
                else:
                    raise e

        multi_thread = self.header.main_streams.unpackinfo.numfolders > 1 and \
            self.header.main_streams.packinfo.numstreams == self.header.main_streams.unpackinfo.numfolders
        fnames = []  # type: List[str]
        for f in self.files:
            # TODO: sanity check
            # check whether f.filename with invalid characters: '../'
            if f.filename.startswith('../'):
                raise Bad7zFile
            # When archive has a multiple files which have same name.
            # To guarantee order of archive, multi-thread decompression becomes off.
            # Currently always overwrite by latter archives.
            # TODO: provide option to select overwrite or skip.
            if f.filename in fnames:
                multi_thread = False
            fnames.append(f.filename)
            if path is not None:
                outfilename = path.joinpath(f.filename)
            else:
                outfilename = pathlib.Path(f.filename)
            if f.is_directory:
                if not outfilename.exists():
                    target_dirs.append(outfilename)
                    target_files.append((outfilename, f.file_properties()))
                else:
                    pass
            elif f.is_socket:
                pass
            elif f.is_symlink:
                buf = io.BytesIO()
                pair = (buf, f.filename)
                target_sym.append(pair)
                self.worker.register_filelike(f.id, buf)
            else:
                self.worker.register_filelike(f.id, outfilename)
                target_files.append((outfilename, f.file_properties()))
        for target_dir in sorted(target_dirs):
            try:
                target_dir.mkdir()
            except FileExistsError:
                if target_dir.is_dir():
                    # skip rare case
                    pass
                elif target_dir.is_file():
                    raise Exception("Directory name is existed as a normal file.")
                else:
                    raise Exception("Directory making fails on unknown condition.")
        self.worker.extract(self.fp, multithread=multi_thread)
        for b, t in target_sym:
            b.seek(0)
            sym_src_org = b.read().decode(encoding='utf-8')
            dirname = os.path.dirname(t)
            if path:
                sym_src = path.joinpath(dirname, sym_src_org)
                sym_dst = path.joinpath(t)
            else:
                sym_src = pathlib.Path(dirname).joinpath(sym_src_org)
                sym_dst = pathlib.Path(t)
            sym_dst.symlink_to(sym_src)
        for o, p in target_files:
            self._set_file_property(o, p)

    def writeall(self, path: Union[pathlib.Path, str], arcname: Optional[str] = None):
        """Write files in target path into archive."""
        if isinstance(path, str):
            path = pathlib.Path(path)
        if path.is_file():
            self.write(path, arcname)
        elif path.is_dir():
            if arcname is not None:
                self.write(path, arcname)
            for nm in sorted(os.listdir(str(path))):
                arc = os.path.join(arcname, nm) if arcname is not None else None
                self.writeall(path.joinpath(nm), arc)

    def write(self, file: Union[pathlib.Path, str], arcname: Optional[str] = None):
        """Write single target file into archive(Not implemented yet)."""
        if isinstance(file, str):
            path = pathlib.Path(file)
        elif isinstance(file, pathlib.Path):
            path = file
        else:
            raise ValueError("Unsupported file type.")
        file_info = self._make_file_info(path, arcname)
        self.files.append(file_info)

    def close(self):
        """Flush all the data into archive and close it.
        When close py7zr start reading target and writing actual archive file.
        """
        if 'w' in self.mode:
            self._write_archive()
        self._fpclose()


# --------------------
# exported functions
# --------------------
def is_7zfile(file: Union[BinaryIO, str, pathlib.Path]) -> bool:
    """Quickly see if a file is a 7Z file by checking the magic number.
    The file argument may be a filename or file-like object too.
    """
    result = False
    try:
        if isinstance(file, io.IOBase) and hasattr(file, "read"):
            result = SevenZipFile._check_7zfile(file)  # type: ignore  # noqa
        elif isinstance(file, str):
            with open(file, 'rb') as fp:
                result = SevenZipFile._check_7zfile(fp)
        elif isinstance(file, pathlib.Path) or isinstance(file, pathlib.PosixPath) or \
                isinstance(file, pathlib.WindowsPath):
            with file.open(mode='rb') as fp:  # type: ignore  # noqa
                result = SevenZipFile._check_7zfile(fp)
        else:
            raise TypeError('invalid type: file should be str, pathlib.Path or BinaryIO, but {}'.format(type(file)))
    except OSError:
        pass
    return result


def unpack_7zarchive(archive, path, extra=None):
    """Function for registering with shutil.register_unpack_archive()"""
    arc = SevenZipFile(archive)
    arc.extractall(path)
    arc.close()
