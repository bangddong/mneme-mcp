import os
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from mneme import index as idx


def get_wiki_dir() -> str:
    return os.getenv("WIKI_DIR", "./wiki")


class WikiEventHandler(FileSystemEventHandler):
    def _to_relative(self, abs_path: str) -> str | None:
        """감시 루트 기준 상대경로. 루트 밖 경로면 None.

        이동(rename)은 감시 루트 밖을 오갈 수 있어 relative_to가 ValueError를 낸다.
        핸들러에서 예외가 나면 옵저버 스레드가 죽으므로 None으로 흘려보낸다.
        """
        try:
            return str(
                Path(abs_path).resolve().relative_to(Path(get_wiki_dir()).resolve())
            ).replace("\\", "/")
        except ValueError:
            return None

    def _index_if_md(self, abs_path: str):
        if not abs_path.endswith(".md"):
            return
        path = self._to_relative(abs_path)
        if path is not None:
            idx.index_file(path, updated_by="human")

    def _remove_if_md(self, abs_path: str):
        if not abs_path.endswith(".md"):
            return
        path = self._to_relative(abs_path)
        if path is not None:
            idx.remove_from_index(path)

    def on_created(self, event):
        if not event.is_directory:
            self._index_if_md(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._index_if_md(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self._remove_if_md(event.src_path)

    def on_moved(self, event):
        """rename/이동: 옛 경로는 인덱스에서 빼고, 새 경로를 인덱싱한다.

        src/dest 확장자는 독립 판정한다 (`.txt`→`.md`, `.md`→`.txt` rename도 처리).
        """
        if event.is_directory:
            self._move_directory(event.src_path, event.dest_path)
            return
        self._remove_if_md(event.src_path)
        self._index_if_md(event.dest_path)

    def _move_directory(self, src_dir: str, dest_dir: str):
        """디렉토리 이동 — 하위 .md 전체의 인덱스 경로가 바뀐다.

        watchdog은 recursive 감시일 때 하위 파일마다 FileMovedEvent를 합성해 주지만,
        그 합성은 *dest*를 walk해서 만들기 때문에 디렉토리가 감시 루트 밖으로 나가면
        하위 항목 이벤트가 오지 않는다(= 옛 경로가 인덱스에 영구 잔류).
        그래서 서브트리를 직접 정산한다. 재인덱싱은 멱등이라 합성 이벤트가 뒤따라 와도
        content_hash 비교로 값싸게 흡수된다.
        """
        src_rel = self._to_relative(src_dir)
        if src_rel is not None:
            prefix = src_rel.rstrip("/") + "/"
            for row in idx.get_all_summaries():
                if row["path"].startswith(prefix):
                    idx.remove_from_index(row["path"])

        if self._to_relative(dest_dir) is None:
            return
        for p in Path(dest_dir).rglob("*.md"):
            self._index_if_md(str(p))


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
