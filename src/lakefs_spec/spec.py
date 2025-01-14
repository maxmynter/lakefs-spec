import hashlib
import io
import logging
import re
import sys
from contextlib import contextmanager
from typing import Any, Generator, Optional

from fsspec.callbacks import NoOpCallback
from fsspec.spec import AbstractBufferedFile, AbstractFileSystem
from fsspec.utils import isfilelike, stringify_path
from lakefs_client.exceptions import (
    ApiException,
    ForbiddenException,
    NotFoundException,
    UnauthorizedException,
)
from lakefs_client.models import ObjectStatsList

from lakefs_spec.client import LakeFSClient
from lakefs_spec.commithook import CommitHook, Default

_DEFAULT_CALLBACK = NoOpCallback()

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler(sys.stdout))

EmptyYield = Generator[None, None, None]


@contextmanager
def translate_exceptions(path: str) -> EmptyYield:
    """
    A context manager to translate lakeFS API exceptions / error codes
    to file exceptions. This is convenience for the user to not have to
    adjust their exception handling to any lakeFS specifics.

    Specifically meant to be applied to lakeFS API endpoints.
    """
    try:
        yield
    except NotFoundException as e:
        raise FileNotFoundError(path) from e
    except ForbiddenException as e:
        raise PermissionError(path) from e
    except UnauthorizedException as e:
        raise PermissionError(f"{path!r} (unauthorized)") from e
    except ApiException as e:
        raise IOError(f"HTTP {e.status}: {e.reason}")


def md5_checksum(lpath: str, blocksize: int = 2**22) -> str:
    with open(lpath, "rb") as f:
        file_hash = hashlib.md5(usedforsecurity=False)
        chunk = f.read(blocksize)
        while chunk:
            file_hash.update(chunk)
            chunk = f.read(blocksize)
    return file_hash.hexdigest()


def parse(path: str) -> tuple[str, str, str]:
    """
    Parses a lakeFS URI in the form ``<repo>/<ref>/<resource>``.

    Parameters
    ----------
    path: str
     String path, needs to conform to the lakeFS URI format described above.
     The ``<resource>`` part can be the empty string.

    Returns
    -------
    str
       A 3-tuple of repository name, reference, and resource name.
    """

    # First regex reflects the lakeFS repository naming rules:
    # only lowercase letters, digits and dash, no leading dash,
    # minimum 3, maximum 63 characters
    # https://docs.lakefs.io/understand/model.html#repository
    # Second regex is the branch: Only letters, digits, underscores
    # and dash, no leading dash
    path_regex = re.compile(r"([a-z0-9][a-z0-9\-]{2,62})/(\w[\w\-]*)/(.*)")
    results = path_regex.fullmatch(path)
    if results is None:
        raise ValueError(f"expected path with structure <repo>/<ref>/<resource>, got {path!r}")

    repo, ref, resource = results.groups()
    return repo, ref, resource


class LakeFSFileSystem(AbstractFileSystem):
    """
    lakeFS file system spec implementation.

    The client is immutable in this implementation, so different users need different
    file systems.
    """

    protocol = "lakefs"

    def __init__(
        self,
        client: LakeFSClient,
        postcommit: bool = False,
        commithook: CommitHook = Default,
        precheck_files: bool = True,
    ):
        """
        The LakeFS file system constructor.

        Parameters
        ----------
        client: LakeFSClient
            The lakeFS client configured for (and authenticated with) the target instance.
        postcommit: bool
            Whether to create lakeFS commits on the chosen branch after mutating operations,
            e.g. uploading or removing a file.
        commithook: CommitHook
            A function taking the fsspec event name (e.g. ``put_file`` for file uploads)
             and the rpath (path relative to the repository root). Must return
             a ``CommitCreation`` object, which is used to create a lakeFS commit
             for the previous file operation. Only applies to mutating operations, and when
             ``postcommit = True``.
        precheck_files: bool
            Whether to compare MD5 checksums of local and remote objects before file
            operations, and skip these operations if checksums match.
        """
        super().__init__()
        self.client = client
        self.postcommit = postcommit
        self.commithook = commithook
        self.precheck_files = precheck_files

    def _rm(self, path):
        raise NotImplementedError

    @classmethod
    def _strip_protocol(cls, path):
        """Copied verbatim from the base class, save for the slash rstrip."""
        if isinstance(path, list):
            return [cls._strip_protocol(p) for p in path]
        path = stringify_path(path)
        protos = (cls.protocol,) if isinstance(cls.protocol, str) else cls.protocol
        for protocol in protos:
            if path.startswith(protocol + "://"):
                path = path[len(protocol) + 3 :]
            elif path.startswith(protocol + "::"):
                path = path[len(protocol) + 2 :]
        # use of root_marker to make minimum required path, e.g., "/"
        return path or cls.root_marker

    @contextmanager
    def scope(
        self, postcommit: Optional[bool] = None, precheck_files: Optional[bool] = None
    ) -> EmptyYield:
        """
        Creates a context manager scope in which the lakeFS file system behavior
        is changed from defaults.

        Either post-write-operation commits, pre-operation checksum verification,
        or both can be selectively enabled or disabled.
        """
        curr_postcommit, curr_precheck_files = self.postcommit, self.precheck_files
        try:
            if postcommit is not None:
                self.postcommit = postcommit
            if precheck_files is not None:
                self.precheck_files = precheck_files
            yield
        finally:
            self.postcommit = curr_postcommit
            self.precheck_files = curr_precheck_files

    def checksum(self, path):
        try:
            return self.info(path).get("checksum", None)
        except FileNotFoundError:
            return None

    def exists(self, path, **kwargs):
        repository, ref, resource = parse(path)
        try:
            self.client.objects.head_object(repository, ref, path)
            return True
        except NotFoundException:
            return False

    def get_file(
        self,
        rpath,
        lpath,
        callback=_DEFAULT_CALLBACK,
        outfile=None,
        **kwargs,
    ):
        # no call to self._strip_protocol here, since that is handled by the
        # AbstractFileSystem.get() implementation
        repository, ref, resource = parse(rpath)

        if self.precheck_files and super().exists(lpath):
            local_checksum = md5_checksum(lpath, blocksize=self.blocksize)
            remote_checksum = self.checksum(rpath)
            if local_checksum == remote_checksum:
                logger.info(
                    f"Skipping download of resource {rpath!r} to local path {lpath!r}: "
                    f"Resource {lpath!r} exists and checksums match."
                )
                return

        if isfilelike(lpath):
            outfile = lpath
        else:
            outfile = open(lpath, "wb")

        try:
            res: io.BufferedReader = self.client.objects.get_object(repository, ref, resource)
            while True:
                chunk = res.read(self.blocksize)
                if not chunk:
                    break
                outfile.write(chunk)
        except NotFoundException:
            raise FileNotFoundError(
                f"resource {resource!r} does not exist on ref {ref!r} in repository {repository!r}"
            )
        except ApiException as e:
            raise FileNotFoundError(f"Error (HTTP{e.status}): {e.reason}") from e
        finally:
            if not isfilelike(lpath):
                outfile.close()

            exc_type, _, __ = sys.exc_info()
            if exc_type:
                from fsspec.implementations.local import LocalFileSystem

                LocalFileSystem().rm_file(lpath)

    def info(self, path, **kwargs):
        path = self._strip_protocol(path)
        out = self.ls(path, detail=True, **kwargs)

        resource = path.split("/", maxsplit=2)
        # input path is a file name
        if len(out) == 1:
            return out[0]
        # input path is a directory name
        elif len(out) > 1:
            return {
                "name": resource,
                "size": sum(o.get("size", 0) for o in out),
                "type": "directory",
            }
        else:
            raise FileNotFoundError(resource)

    def ls(self, path, detail=True, amount=100, **kwargs):
        path = self._strip_protocol(path)
        repository, ref, prefix = parse(path)

        has_more, after = True, ""
        # stat infos are either the path only (`detail=False`) or a dict full of metadata
        info: list[Any] = []

        while has_more:
            try:
                res: ObjectStatsList = self.client.objects.list_objects(
                    repository,
                    ref,
                    user_metadata=detail,
                    after=after,
                    prefix=prefix,
                    amount=amount,
                )
            except NotFoundException:
                raise FileNotFoundError(
                    f"resource {prefix!r} does not exist on ref {ref!r} "
                    f"in repository {repository!r}"
                )
            has_more, after = res.pagination.has_more, res.pagination.next_offset
            for obj in res.results:
                info.append(
                    {
                        "checksum": obj.checksum,
                        "content-type": obj.content_type,
                        "mtime": obj.mtime,
                        "name": obj.path,
                        "size": obj.size_bytes,
                        "type": "file",
                    }
                )

        if not detail:
            return [o["name"] for o in info]
        return info

    def _open(
        self,
        path,
        mode="rb",
        block_size=None,
        autocommit=True,
        cache_options=None,
        **kwargs,
    ):
        if mode != "rb":
            raise NotImplementedError("only mode='rb' is supported for open()")

        return LakeFSFile(
            self,
            path=path,
            mode=mode,
            block_size=block_size,
            autocommit=autocommit,
            cache_options=cache_options,
            **kwargs,
        )

    def put_file(
        self,
        lpath,
        rpath,
        callback=_DEFAULT_CALLBACK,
        **kwargs,
    ):
        repository, branch, resource = parse(rpath)

        if self.precheck_files:
            # TODO (n.junge): Make this work for lpaths that are themselves lakeFS paths
            local_checksum = md5_checksum(lpath, blocksize=self.blocksize)
            remote_checksum = self.checksum(rpath)
            if local_checksum == remote_checksum:
                logger.info(
                    f"Skipping upload of resource {lpath!r} to remote path {rpath!r}: "
                    f"Resource {rpath!r} exists and checksums match."
                )
                return

        with open(lpath, "rb") as f:
            self.client.objects.upload_object(
                repository=repository, branch=branch, path=resource, content=f
            )

    def put(
        self,
        lpath,
        rpath,
        recursive=False,
        callback=_DEFAULT_CALLBACK,
        maxdepth=None,
        **kwargs,
    ):
        super().put(
            lpath, rpath, recursive=recursive, callback=callback, maxdepth=maxdepth, **kwargs
        )

        if self.postcommit:
            # TODO: This only works for string rpaths, fsspec allows rpath lists
            repository, branch, resource = parse(rpath)
            commit_creation = self.commithook("put", resource)
            self.client.commits.commit(
                repository=repository, branch=branch, commit_creation=commit_creation
            )

    def rm_file(self, path):
        repository, branch, resource = parse(path)

        try:
            self.client.objects.delete_object(repository=repository, branch=branch, path=resource)
        except NotFoundException:
            raise FileNotFoundError(
                f"object {resource!r} does not exist on branch {branch!r} "
                f"in repository {repository!r}"
            )

    def rm(self, path, recursive=False, maxdepth=None):
        super().rm(path, recursive=recursive, maxdepth=maxdepth)

        if self.postcommit:
            repository, branch, resource = parse(path)
            commit_creation = self.commithook("rm", resource)
            self.client.commits.commit(
                repository=repository, branch=branch, commit_creation=commit_creation
            )


class LakeFSFile(AbstractBufferedFile):
    """lakeFS file implementation. Currently read-only."""

    def __init__(
        self,
        fs,
        path,
        mode="rb",
        block_size="default",
        autocommit=True,
        cache_type="readahead",
        cache_options=None,
        size=None,
        **kwargs,
    ):
        super().__init__(
            fs,
            path,
            mode=mode,
            block_size=block_size,
            autocommit=autocommit,
            cache_type=cache_type,
            cache_options=cache_options,
            size=size,
            **kwargs,
        )

    def _upload_chunk(self, final=False):
        # Possibly blocked by https://github.com/treeverse/lakeFS/issues/6259
        raise NotImplementedError

    def _initiate_upload(self):
        # Possibly blocked by https://github.com/treeverse/lakeFS/issues/6259
        raise NotImplementedError

    def _fetch_range(self, start: int, end: int) -> bytes:
        repository, ref, resource = parse(self.path)
        try:
            res: io.BufferedReader = self.fs.client.objects.get_object(
                repository, ref, resource, range=f"bytes={start}-{end - 1}"
            )
            return res.read()
        except ApiException as e:
            raise FileNotFoundError(f"Error (HTTP{e.status}): {e.reason}") from e
