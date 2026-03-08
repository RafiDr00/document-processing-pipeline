"""
Excel export service.

Generates Excel (.xlsx) files from extracted document data.
Uses openpyxl via pandas for formatting and export.
"""

import os
import uuid
from typing import Any

import pandas as pd

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()


class ExcelExportError(Exception):
    """Raised when Excel export fails."""
    pass


class ExcelExporter:
    """
    Exports extracted document records to formatted Excel files.

    Features:
    - Auto-column-width formatting
    - Smart sorting by common fields
    - Handles empty datasets gracefully
    """

    SORT_PRIORITY_COLUMNS = ["Name", "Client Name", "Date", "Invoice No", "ID"]

    def __init__(self, export_dir: str | None = None):
        self.export_dir = export_dir or settings.EXPORT_DIR
        os.makedirs(self.export_dir, exist_ok=True)

    def export(
        self,
        records: list[dict[str, Any]],
        filename: str | None = None,
        sheet_name: str = "Extracted Data",
    ) -> str:
        """
        Export records to an Excel file.

        Args:
            records: List of record dicts to export.
            filename: Output filename (auto-generated if None).
            sheet_name: Excel worksheet name.

        Returns:
            Absolute path to the generated Excel file.
        """
        if not records:
            raise ExcelExportError("No records to export.")

        if filename is None:
            filename = f"export_{uuid.uuid4().hex[:12]}.xlsx"

        output_path = os.path.join(self.export_dir, filename)

        try:
            df = pd.DataFrame(records)

            # Smart sort by available priority columns
            sort_cols = [col for col in self.SORT_PRIORITY_COLUMNS if col in df.columns]
            if sort_cols:
                df = df.sort_values(by=sort_cols, na_position="last")

            # Write to Excel with formatting
            with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
                df.to_excel(writer, index=False, sheet_name=sheet_name)

                # Auto-adjust column widths
                worksheet = writer.sheets[sheet_name]
                for col_idx, column in enumerate(df.columns, start=1):
                    max_len = max(
                        df[column].astype(str).map(len).max(),
                        len(str(column)),
                    )
                    # Cap column width at 60 characters
                    adjusted_width = min(max_len + 4, 60)
                    worksheet.column_dimensions[
                        worksheet.cell(row=1, column=col_idx).column_letter
                    ].width = adjusted_width

            logger.info(
                "Excel export completed",
                extra={
                    "output_path": output_path,
                    "record_count": len(records),
                    "columns": list(df.columns),
                },
            )

            return output_path

        except ExcelExportError:
            raise
        except Exception as e:
            raise ExcelExportError(f"Failed to generate Excel file: {e}") from e
