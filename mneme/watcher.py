import os
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from mneme import index as idx


def get_wiki_dir() -> str:
    return os.getenv("WIKI_DIR", "./wiki")


class WikiEventHandler(FileSystemEventHandler):
    def _to_relative(self, abs_path: str) -> str:
        return str(Path(abs_path).relative_to(get_wiki_dir())).replace("\\", "/")

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            path = self._to_relative(event.src_path)
            idx.index_file(path, updated_by="human")

    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            path = self._to_relative(event.src_path)
            idx.index_file(path, updated_by="human")

    def on_deleted(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            path = self._to_relative(event.src_path)
            idx.remove_from_index(path)


_observer: Observer | None = None


def start_watcher():
    global _observer
    wiki_dir = get_wiki_dir()
    Path(wiki_dir).mkdir(parents=True, exist_ok=True)

    _observer = Observer()
    _observer.schedule(WikiEventHandler(), wiki_dir, recursive=True)
    _observer.start()


def stop_watcher():
    global _observer
    if _observer:
        _observer.stop()
        _observer.join()
        _observer = None
