#!/usr/bin/env python3
"""
Commander Land Base Generator -- Web UI
----------------------------------------
A thin Flask front end over get_lands.py's generate_land_base(). Fill in
the form, submit, and the page reloads with the same report the CLI
prints, plus a one-click copy button for the Archidekt import list.

Usage:
    python web_app.py
    (then open http://127.0.0.1:5000 in a browser)
"""

from flask import Flask, render_template, request

from get_lands import (
    CommanderNotFoundError,
    DEFAULT_EDHREC_POOL,
    DEFAULT_MIN_BASICS,
    DEFAULT_TOTAL_LANDS,
    NotACommanderError,
    generate_land_base,
    inclusion_pct,
)

app = Flask(__name__)


def _int_field(form, name, default):
    raw = form.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"'{name}' must be a whole number, got '{raw}'.")


@app.route("/", methods=["GET", "POST"])
def index():
    form_values = {
        "commander": "",
        "lands": DEFAULT_TOTAL_LANDS,
        "edhrec_pool": DEFAULT_EDHREC_POOL,
        "utility_pool": DEFAULT_EDHREC_POOL,
        "min_basics": DEFAULT_MIN_BASICS,
        "snow_basics": False,
    }
    result = None
    error = None
    log_lines = []

    if request.method == "POST":
        form_values["commander"] = request.form.get("commander", "").strip()
        form_values["snow_basics"] = request.form.get("snow_basics") == "on"
        try:
            form_values["lands"] = _int_field(request.form, "lands", DEFAULT_TOTAL_LANDS)
            form_values["edhrec_pool"] = _int_field(request.form, "edhrec_pool", DEFAULT_EDHREC_POOL)
            form_values["utility_pool"] = _int_field(request.form, "utility_pool", DEFAULT_EDHREC_POOL)
            form_values["min_basics"] = _int_field(request.form, "min_basics", DEFAULT_MIN_BASICS)

            if not form_values["commander"]:
                raise ValueError("Please enter a commander name.")

            result = generate_land_base(
                form_values["commander"],
                form_values["lands"],
                edhrec_pool=form_values["edhrec_pool"],
                utility_pool=form_values["utility_pool"],
                min_basics=form_values["min_basics"],
                use_snow_basics=form_values["snow_basics"],
                log=log_lines.append,
            )
        except (CommanderNotFoundError, NotACommanderError) as e:
            error = str(e)
        except ValueError as e:
            error = str(e)

    return render_template(
        "index.html",
        form_values=form_values,
        result=result,
        error=error,
        log_lines=log_lines,
        inclusion_pct=inclusion_pct,
    )


if __name__ == "__main__":
    app.run(debug=True)
