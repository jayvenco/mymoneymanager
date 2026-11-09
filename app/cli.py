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

    @app.cli.command("seed-demo")
    def seed_demo_command() -> None:
        """Maak een demo-huishouden met 12 maanden Nederlandse transacties (lege database)."""
        from .services.demo import DEMO_PASSWORD, DEMO_USERNAME, DemoError, seed_demo

        try:
            result = seed_demo()
        except DemoError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(
            f"Demo aangemaakt: {result['transacties']} transacties, {result['rekeningen']} rekeningen."
        )
        click.echo(f"Inloggen: {DEMO_USERNAME} / {DEMO_PASSWORD}  (alleen voor demo-doeleinden)")
