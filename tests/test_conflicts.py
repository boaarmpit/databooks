from pathlib import Path

from py._path.local import LocalPath

from databooks.conflicts import path2conflicts
from databooks.data_models.cell import BaseCell, CellMetadata
from databooks.data_models.notebook import NotebookMetadata
from tests.test_data_models.test_notebook import TestJupyterNotebook
from tests.test_git_utils import ConflictFile, init_repo_conflicts


def test_path2diff(tmpdir: LocalPath) -> None:
    """Return a DiffFile based on a path and git conflicts."""
    notebook_main = TestJupyterNotebook().jupyter_notebook
    notebook_other = TestJupyterNotebook().jupyter_notebook

    notebook_main.metadata = NotebookMetadata(
        kernelspec=dict(
            display_name="different_kernel_display_name", name="kernel_name"
        ),
        field_to_remove=["Field to remove"],
        another_field_to_remove="another field",
    )
    extra_cell = BaseCell(
        cell_type="raw",
        metadata=CellMetadata(random_meta=["meta"]),
        source="extra",
    )
    notebook_other.cells = notebook_other.cells + [extra_cell]

    nb_filepath = Path("test_notebook.ipynb")

    git_repo = init_repo_conflicts(
        tmpdir=tmpdir,
        filename=nb_filepath,
        contents_main=notebook_main.json(),
        contents_other=notebook_other.json(),
        commit_message_main="Commit message from main",
        commit_message_other="Commit message from other",
    )

    assert isinstance(git_repo.working_dir, (Path, str))

    conflict_files = path2conflicts(nb_paths=[nb_filepath], repo=git_repo)

    assert len(conflict_files) == 1

    conflict_file = conflict_files[0]
    assert isinstance(conflict_file, ConflictFile)
    assert conflict_file.filename == (git_repo.working_dir / nb_filepath)
    assert conflict_file.first_contents == notebook_main.json()
    assert conflict_file.last_contents == notebook_other.json()

    # We use git logs for ids, which start with a hash that won't match
    assert conflict_file.first_log.endswith("Commit message from main")
    assert conflict_file.last_log.endswith("Commit message from other")
