import os

from datasette.utils import to_css_class, validate_sql_select
from datasette.utils.asgi import AsgiFileDownload

from .base import DatasetteError, DataView


class DatabaseView(DataView):
    name = "database"

    async def data(self, request, database, hash, default_labels=False, _size=None):
        if request.args.get("sql"):
            if not self.ds.config("allow_sql"):
                raise DatasetteError("sql= is not allowed", status=400)
            sql = request.raw_args.pop("sql")
            validate_sql_select(sql)
            return await self.custom_sql(request, database, hash, sql, _size=_size)

        db = self.ds.databases[database]

        table_counts = await db.table_counts(5)
        views = await db.view_names()
        hidden_table_names = set(await db.hidden_table_names())
        all_foreign_keys = await db.get_all_foreign_keys()

        metadata = (self.ds.metadata("databases") or {}).get(database, {})
        self.ds.update_with_inherited_metadata(metadata)

        tables = []
        for table in table_counts:
            table_columns = await db.table_columns(table)
            tables.append(
                {
                    "name": table,
                    "columns": table_columns,
                    "primary_keys": await db.primary_keys(table),
                    "count": table_counts[table],
                    "hidden": table in hidden_table_names,
                    "fts_table": await db.fts_table(table),
                    "foreign_keys": all_foreign_keys[table],
                }
            )

        tables.sort(key=lambda t: (t["hidden"], t["name"]))
        return (
            {
                "database": database,
                "size": db.size,
                "tables": tables,
                "hidden_count": len([t for t in tables if t["hidden"]]),
                "views": views,
                "queries": self.ds.get_canned_queries(database),
            },
            {
                "show_hidden": request.args.get("_show_hidden"),
                "editable": True,
                "metadata": metadata,
                "allow_download": self.ds.config("allow_download")
                and not db.is_mutable
                and database != ":memory:",
            },
            ("database-{}.html".format(to_css_class(database)), "database.html"),
        )


class DatabaseDownload(DataView):
    name = "database_download"

    async def view_get(self, request, database, hash, correct_hash_present, **kwargs):
        if database not in self.ds.databases:
            raise DatasetteError("Invalid database", status=404)
        db = self.ds.databases[database]
        if db.is_memory:
            raise DatasetteError("Cannot download :memory: database", status=404)
        if not self.ds.config("allow_download") or db.is_mutable:
            raise DatasetteError("Database download is forbidden", status=403)
        if not db.path:
            raise DatasetteError("Cannot download database", status=404)
        filepath = db.path
        return AsgiFileDownload(
            filepath,
            filename=os.path.basename(filepath),
            content_type="application/octet-stream",
        )
