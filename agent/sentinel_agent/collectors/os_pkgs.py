"""Debian/Ubuntu packages via dpkg-query."""

from sentinel_agent.collectors import run

# db:Status-Abbrev is two or three characters; "ii" means desired=install,
# current=installed. Anything else (rc = removed-but-config-remains, iU, etc.)
# is not an installed package and must not be reported as one.
_FORMAT = "${db:Status-Abbrev}\\t${Package}\\t${Version}\\n"


def parse(stdout: str) -> list[dict]:
    packages: list[dict] = []
    for line in stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            continue
        status, name, version = (field.strip() for field in fields)
        if status != "ii" or not name or not version:
            continue
        packages.append(
            {
                "ecosystem": "os",
                "name": name,
                "version": version,
                "scope": "system",
                "meta": {},
            }
        )
    return packages


def collect() -> list[dict]:
    return parse(run(["dpkg-query", "-W", f"-f={_FORMAT}"], timeout=60))
