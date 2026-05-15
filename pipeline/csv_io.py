import csv
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel, ValidationError

from pipeline.atomic_write import write_atomic  # F-014: shared atomic-write idiom


class CsvIoError(Exception):
    pass


class CorruptCsvError(CsvIoError):
    def __init__(self, path: Path, row_number: int | None, details: str):
        self.path = path
        self.row_number = row_number
        self.details = details
        super().__init__(self.__str__())

    def __str__(self) -> str:
        if self.row_number is None:
            return f"Corrupt CSV at {self.path}: {self.details}"
        return f"Corrupt CSV at {self.path} (row {self.row_number}): {self.details}"


class RowNotFoundError(CsvIoError):
    def __init__(self, ucd_fil_id: str):
        self.ucd_fil_id = ucd_fil_id
        super().__init__(f"Row not found: ucd_fil_id={ucd_fil_id!r}")


class DuplicateRowError(CsvIoError):
    def __init__(self, ucd_fil_id: str):
        self.ucd_fil_id = ucd_fil_id
        super().__init__(f"Row already exists: ucd_fil_id={ucd_fil_id!r}")


class Transaction:
    def __init__(
        self,
        rows: list[BaseModel],
        columns: tuple[str, ...],
        row_model: type[BaseModel],
    ):
        self._rows: list[BaseModel] = list(rows)
        self._columns = columns
        self._row_model = row_model
        self._field_names = set(row_model.model_fields.keys())
        self._dirty = False

    @property
    def dirty(self) -> bool:
        return self._dirty

    def read_all(self) -> list[BaseModel]:
        return list(self._rows)

    def find(self, ucd_fil_id: str) -> BaseModel | None:
        for r in self._rows:
            if getattr(r, "ucd_fil_id") == ucd_fil_id:
                return r
        return None

    def append(self, row: BaseModel) -> None:
        if not isinstance(row, self._row_model):
            raise TypeError(
                f"append expects {self._row_model.__name__}, got {type(row).__name__}"
            )
        if self.find(row.ucd_fil_id) is not None:
            raise DuplicateRowError(row.ucd_fil_id)
        self._rows.append(row)
        self._dirty = True

    def update(self, ucd_fil_id: str, **fields) -> BaseModel:
        unknown = set(fields) - self._field_names
        if unknown:
            raise ValueError(
                f"Unknown field(s) for {self._row_model.__name__}: "
                f"{sorted(unknown)}. Known: {sorted(self._field_names)}"
            )
        for i, r in enumerate(self._rows):
            if r.ucd_fil_id == ucd_fil_id:
                proposed = r.model_copy(update=fields)
                validated = self._row_model.model_validate(proposed.model_dump())
                self._rows[i] = validated
                self._dirty = True
                return validated
        raise RowNotFoundError(ucd_fil_id)


class CsvTable:
    def __init__(
        self,
        path: Path,
        columns: tuple[str, ...],
        row_model: type[BaseModel],
    ):
        self.path = Path(path)
        self.columns = tuple(columns)
        self.row_model = row_model

    def _lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    def _read_rows(self) -> list[BaseModel]:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return []
        with self.path.open("r", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != list(self.columns):
                raise CorruptCsvError(
                    self.path,
                    None,
                    f"header mismatch: expected {list(self.columns)}, "
                    f"got {reader.fieldnames}",
                )
            rows: list[BaseModel] = []
            for i, raw in enumerate(reader, start=1):
                try:
                    rows.append(self.row_model.from_csv_dict(raw))
                except ValidationError as e:
                    raise CorruptCsvError(self.path, i, str(e)) from e
            return rows

    def snapshot(self) -> list[BaseModel]:
        return self._read_rows()

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        lock_path = self._lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            rows = self._read_rows()
            tx = Transaction(rows, self.columns, self.row_model)
            yield tx
            if tx.dirty:
                self._commit(tx)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def _commit(self, tx: Transaction) -> None:
        # F-014: tempfile + fsync + os.replace + cleanup-on-exception lives
        # in pipeline.atomic_write; this method only specifies the row-
        # serialization shape via the writer callback.
        def _write_csv(f) -> None:
            writer = csv.DictWriter(
                f, fieldnames=list(self.columns), lineterminator="\n"
            )
            writer.writeheader()
            for r in tx.read_all():
                writer.writerow(r.to_csv_dict())

        write_atomic(self.path, _write_csv)
