#!/bin/sh
# Run the dev image with a throwaway Postgres beside it, on a private network: the state is
# the music database's, so the tests need one (homelab#2502). The arguments are docker run's,
# after --rm --network and MUSIC_TEST_DATABASE_URL. Used by `just test` and `just test-mcp`.
# The image is CI's (.github/workflows/service.yml): move both with the CNPG major.
set -eu
net="music-test-$$"
docker network create "$net" >/dev/null
trap 'docker rm -f "$net-pg" >/dev/null 2>&1; docker network rm "$net" >/dev/null 2>&1' EXIT
docker run -d --rm --name "$net-pg" --network "$net" -e POSTGRES_PASSWORD=test \
    postgres:17.11@sha256:31ba9c94a03ada9988f2bb493a22a75eef3a185884533e5cdbcd498ea835b08f >/dev/null
until docker exec "$net-pg" pg_isready -q -h 127.0.0.1 -U postgres; do sleep 1; done
docker run --rm --network "$net" -e MUSIC_TEST_DATABASE_URL="postgresql://postgres:test@$net-pg:5432/postgres" "$@"
