"""Saved 104 cover-letter (自我推薦信) patterns — list / upsert / delete.

Reusable named templates for the tw104_apply automation, stored globally via
settings_store. Authenticated users only (router is mounted behind require_user
in main.py); they're plain user content, not admin config.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src import settings_store

router = APIRouter()


class CoverLetter(BaseModel):
    name: str
    text: str = ""


@router.get("/cover-letters")
def list_cover_letters() -> list[dict]:
    return settings_store.get_cover_letters()


@router.post("/cover-letters")
def save_cover_letter(data: CoverLetter) -> list[dict]:
    try:
        return settings_store.save_cover_letter(data.name, data.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/cover-letters/{name}")
def delete_cover_letter(name: str) -> list[dict]:
    return settings_store.delete_cover_letter(name)
