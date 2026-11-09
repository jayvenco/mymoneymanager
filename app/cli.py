"""CLI-commando's (`flask ...`)."""

import click
from flask import Flask


def register_cli(app: Flask) -> None:
    @app.cli.command("version")
    def version() -> None:
        """Toon versie-informatie."""
        click.echo(
            f"{app.config['APP_VERSION']} ({app.config['GIT_SHA']}) {app.config['BUILD_DATE']}"
        )
