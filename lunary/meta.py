from contextvars import ContextVar

meta_ctx = ContextVar("meta_ctx", default=None)


class MetaContextManager:
    def __init__(self, meta: dict):
        meta_ctx.set(meta)

    def __enter__(self):
        return self

    def set_value_by_key(self, key: str, val: any):
        meta = meta_ctx.get()
        meta[key] = val
        meta_ctx.set(meta)

    def unset_key(self, key: str):
        meta = meta_ctx.get()
        if key in meta:
            del meta[key]
        meta_ctx.set(meta)

    def __exit__(self, exc_type, exc_value, exc_tb):
        meta_ctx.set(None)


def meta(meta: dict) -> MetaContextManager:
    return MetaContextManager(meta)