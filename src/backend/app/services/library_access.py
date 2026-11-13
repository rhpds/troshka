"""Shared library-use access predicate for API listings and managed source reads."""

from sqlalchemy import or_

from app.models.library import Library, LibraryItem, LibraryShare


def usable_items(db, user):
    query = db.query(LibraryItem)
    if user.role == "admin":
        return query
    libraries = db.query(Library.id).filter(
        or_(
            Library.type == "central",
            (Library.type == "personal") & (Library.owner_id == user.id),
        )
    )
    shares = db.query(LibraryShare.item_id).filter_by(shared_with_id=user.id)
    return query.filter(
        or_(LibraryItem.library_id.in_(libraries), LibraryItem.id.in_(shares))
    )
