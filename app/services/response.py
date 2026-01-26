def list_response(items: list, limit: int, offset: int) -> dict:
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


class ListResponseMixin:
    @classmethod
    def list_response(cls, db, *args, **kwargs):
        if "limit" in kwargs and "offset" in kwargs:
            limit = kwargs["limit"]
            offset = kwargs["offset"]
            items = cls.list(db, *args, **kwargs)
        else:
            if len(args) < 2:
                raise ValueError("limit and offset are required for list responses")
            *list_args, limit, offset = args
            items = cls.list(db, *list_args, limit=limit, offset=offset, **kwargs)
        return list_response(items, limit, offset)
