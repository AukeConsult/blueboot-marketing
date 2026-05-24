#!/usr/bin/env bash
set -e
[ -f .env ] || cp .env.example .env
docker compose up --build
