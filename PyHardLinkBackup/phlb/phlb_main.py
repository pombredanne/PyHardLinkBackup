#!/usr/bin/python3

"""
    Python HardLink Backup
    ~~~~~~~~~~~~~~~~~~~~~~

    :copyleft: 2015-2016 by Jens Diemer
    :license: GNU GPL v3 or above, see LICENSE for more details.
"""

import hashlib
import logging
import os
import shutil
import sys

# time.clock() on windows and time.time() on linux
from timeit import default_timer

try:
    # https://github.com/tqdm/tqdm
    from tqdm import tqdm
except ImportError as err:
    raise ImportError("Please install 'tqdm': %s" % err)


log = logging.getLogger("phlb.%s" % __name__)
#logging.basicConfig(level=logging.DEBUG)


# os.environ["DJANGO_SETTINGS_MODULE"] = "PyHardLinkBackup.django_project.settings"
import django


from PyHardLinkBackup.phlb import os_scandir
from PyHardLinkBackup.phlb.config import phlb_config
from PyHardLinkBackup.phlb.human import human_time, human_filesize
from PyHardLinkBackup.backup_app.models import BackupEntry
from PyHardLinkBackup.phlb.path_helper import PathHelper


class FileBackup(object):
    def __init__(self, file_entry, path_helper):
        self.file_entry = file_entry # os.DirEntry() instance
        self.path = path_helper # PathHelper(backup_root) instance

    def _deduplication_backup(self, file_entry, in_file, out_file, process_bar):
        hash = hashlib.new(phlb_config.hash_name)
        while True:
            data = in_file.read(phlb_config.chunk_size)
            if not data:
                break

            out_file.write(data)
            hash.update(data)
            process_bar.update(len(data))
        return hash

    def deduplication_backup(self, process_bar):
        src_path = self.file_entry.path
        log.debug("*** deduplication backup: '%s'", src_path)

        self.path.set_src_filepath(src_path)
        log.debug("abs_src_filepath: '%s'", self.path.abs_src_filepath)
        log.debug("abs_dst_filepath: '%s'", self.path.abs_dst_filepath)
        log.debug("abs_dst_hash_filepath: '%s'", self.path.abs_dst_hash_filepath)
        log.debug("abs_dst_dir: '%s'", self.path.abs_dst_path)

        if not os.path.isdir(self.path.abs_dst_path):
            os.makedirs(
                self.path.abs_dst_path,
                mode=phlb_config.default_new_path_mode
            )
        else:
            assert not os.path.isfile(self.path.abs_dst_filepath), "Out file already exists: %r" % self.path.abs_src_filepath

        try:
            with open(self.path.abs_src_filepath, "rb") as in_file:
                with open(self.path.abs_dst_hash_filepath, "w") as hash_file:
                    with open(self.path.abs_dst_filepath, "wb") as out_file:
                        hash = self._deduplication_backup(self.file_entry, in_file, out_file, process_bar)
                    hash_hexdigest = hash.hexdigest()
                    hash_file.write(hash_hexdigest)
        except KeyboardInterrupt:
            os.remove(self.path.abs_dst_filepath)
            os.remove(self.path.abs_dst_hash_filepath)
            sys.exit(-1)

        temp_bak_name=self.path.abs_dst_filepath+".bak" # FIXME

        old_backups = BackupEntry.objects.filter(
            content_info__hash_hexdigest=hash_hexdigest,
            no_link_source=False,
        )
        file_linked = False
        for old_backup in old_backups:
            log.debug("+++ old: '%s'", old_backup)
            abs_old_backup_path = old_backup.get_backup_path()
            if not os.path.isfile(abs_old_backup_path):
                log.error("*** ERROR old file doesn't exist! '%s'", abs_old_backup_path)
                continue

            assert abs_old_backup_path != self.path.abs_dst_filepath

            # TODO: compare hash / current content before replace with a link

            os.rename(self.path.abs_dst_filepath, temp_bak_name) # FIXME
            try:
                os.link(abs_old_backup_path, self.path.abs_dst_filepath)
            except OSError as err:
                os.rename(temp_bak_name, self.path.abs_dst_filepath) # FIXME
                log.error("Can't link '%s' to '%s': %s" % (
                    abs_old_backup_path, self.path.abs_dst_filepath, err
                ))
                log.info("Mark %r with 'no link source'.", old_backup)
                old_backup.no_link_source=True
                old_backup.save()
            else:
                os.remove(temp_bak_name) # FIXME
                file_linked = True
                log.info("Replaced with a hardlink to: '%s'" % abs_old_backup_path)
                break

        file_stat=self.file_entry.stat()

        BackupEntry.objects.create(
            self.path.backup_run,
            directory=self.path.sub_path,
            filename=self.path.filename,
            hash_hexdigest=hash_hexdigest,
            file_stat=file_stat,
        )

        # set origin access/modified times to the new created backup file
        atime_ns = file_stat.st_atime_ns
        mtime_ns = file_stat.st_mtime_ns
        os.utime(self.path.abs_dst_filepath, ns=(atime_ns, mtime_ns))

        return file_linked, file_stat.st_size


class HardLinkBackup(object):
    def __init__(self, src_path):
        self.start_time = default_timer()
        self.path = PathHelper(src_path)

        print("Backup to: '%s'" % self.path.abs_dst_root)
        os.makedirs(
            self.path.abs_dst_root,
            mode=phlb_config.default_new_path_mode,
            exist_ok=True
        )
        if not os.path.isdir(self.path.abs_dst_root):
            raise OSError(
                "Backup path '%s' doesn't exists!" % self.path.abs_dst_root
            )

        with open(self.path.summary_filepath, "w") as summary_file:
            summary_file.write("Start backup: %s\n\n" % self.path.time_string)
            summary_file.write("Source: %s\n\n" % self.path.abs_src_root)

            self.setup_logging()

            try:
                self._backup()
            except KeyboardInterrupt:
                print("\nCleanup after keyboard interrupt:")

                print("\t* clean '%s'" % self.path.abs_dst_root)
                def print_error(fn, path, excinfo):
                    print("\tError remove: '%s'" % path)
                shutil.rmtree(self.path.abs_dst_root, ignore_errors=True, onerror=print_error)

                # TODO: Remove unused ForeignKey, too,
                queryset = BackupEntry.objects.filter(backup_run=self.path.backup_run)
                count = queryset.count()
                print("\t* cleanup %i database entries" % count)
                queryset.delete()

                print("Bye")
                sys.exit(1)

            summary_file.write("\n".join(self.get_summary()))


    def setup_logging(self):

        level = phlb_config.logging_level
        if level>=logging.DEBUG:
            # use root logger
            logger = logging.getLogger()
        else:
            logger = logging.getLogger("phlb")

        logger.setLevel(level=level)
        logger.handlers = []
        logger.addHandler(logging.StreamHandler())
        logger.addHandler(logging.FileHandler(self.path.log_filepath))

        logger.info("Set log level to %i and log into: %r" % (level, self.path.log_filepath))

    def _scandir(self, path):
        file_list = []
        total_size = 0
        start_time = default_timer()
        print("\nScan '%s'...\n" % path)

        skip_dirs = phlb_config.skip_dirs
        skip_files = phlb_config.skip_files
        print("Scan with skip dirs: %s" % repr(skip_dirs))
        print("Scan with skip files: %s" % repr(skip_files))
        fs_iterator = os_scandir.walk2(path, skip_dirs, skip_files)

        for top, dirs, nondirs in tqdm(fs_iterator, unit="dirs", leave=True):
            for entry in nondirs:
                if entry.is_file():
                    file_list.append(entry)
                    total_size += entry.stat().st_size
                else:
                    raise NotImplementedError("todo: %r", entry)

        self.file_count = len(file_list)
        self.total_size = total_size

        print("\nscanned %i files in %s\n" % (
            self.file_count, human_time(default_timer()-start_time)
        ))
        return file_list

    def _backup(self):
        file_list = self._scandir(self.path.abs_src_root)
        self.file_count = len(file_list)

        print("%s in %i files to backup." % (
            human_filesize(self.total_size), self.file_count,
        ))

        self.total_file_link_count = 0
        self.total_stined_bytes = 0
        self.total_new_file_count = 0
        self.total_new_bytes = 0
        with tqdm(total=self.total_size, unit='B', unit_scale=True) as process_bar:
            for no, file_entry in enumerate(file_list):
                log.debug("%i '%s'", no, file_entry.path)

                file_backup = FileBackup(file_entry, self.path)
                file_linked, file_size = file_backup.deduplication_backup(process_bar)
                if file_linked:
                    # os.link() was used
                    self.total_file_link_count += 1
                    self.total_stined_bytes += file_size
                else:
                    self.total_new_file_count += 1
                    self.total_new_bytes += file_size

        self.duration = default_timer() - self.start_time

    def get_summary(self):
        def to_percent(part, total):
            try:
                return part/total*100
            except ZeroDivisionError:
                # e.g.: Backup only 0-Bytes files ;)
                return 0

        summary = ["Backup done:"]
        summary.append(" * Files to backup: %i files" % self.file_count)
        summary.append(" * Source file sizes: %s" % human_filesize(self.total_size))
        summary.append(" * new content to saved: %i files (%s %.1f%%)" % (
            self.total_new_file_count,
            human_filesize(self.total_new_bytes),
            to_percent(self.total_new_bytes, self.total_size)
        ))
        summary.append(" * stint space via hardlinks: %i files (%s %.1f%%)" % (
            self.total_file_link_count,
            human_filesize(self.total_stined_bytes),
            to_percent(self.total_stined_bytes, self.total_size)
        ))
        performance = self.total_size / self.duration / 1024.0 / 1024.0
        summary.append(" * duration: %s %.1fMB/s\n" % (human_time(self.duration), performance))
        return summary

    def print_summary(self):
        print("\n%s\n" % "\n".join(self.get_summary()))


def backup(path):
    django.setup()
    phlb = HardLinkBackup(src_path=path)
    phlb.print_summary()

if __name__ == '__main__':
    src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    # src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "django_project")

    print("\n*** Test run with: '%s' ***\n" % src_path)

    phlb = HardLinkBackup(src_path=src_path)
    phlb.print_summary()



