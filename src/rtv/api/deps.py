"""Dependency accessors pulling shared services off ``app.state``."""

from __future__ import annotations

from fastapi import Request

from rtv.coaching.features import CoachingService
from rtv.services import AppServices
from rtv.store.repository import Repository


def get_services(request: Request) -> AppServices:
    return request.app.state.services


def get_repo(request: Request) -> Repository:
    return request.app.state.services.repo


def get_coaching(request: Request) -> CoachingService:
    return request.app.state.services.coaching
