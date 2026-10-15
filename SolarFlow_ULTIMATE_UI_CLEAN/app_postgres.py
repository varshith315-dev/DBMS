from flask import Flask, render_template, request, redirect, url_for, flash
import psycopg
from psycopg.rows import dict_row
from datetime import datetime
import os
import re

app = Flask(__name__)
app.secret_key = os.getenv("SOLARFLOW_SECRET", "solarflow-development")

DATABASE_URL = os.getenv("DATABASE_URL")


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured. Add the Aiven PostgreSQL connection string in your environment.")
    return psycopg.connect(DATABASE_URL, sslmode="require", row_factory=dict_row)


def postgres_sql(sql):
    """Translate the small Oracle-SQL subset used by this app to PostgreSQL."""
    sql = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", r"%(\1)s", sql)
    sql = re.sub(r"\bNVL\s*\(", "COALESCE(", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bSYSTIMESTAMP\b", "CURRENT_TIMESTAMP", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bSYSDATE\b", "CURRENT_DATE", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\s+FROM\s+dual\b", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"TRUNC\(([^()]+)\)\s*-\s*TRUNC\(CURRENT_DATE\)", r"(\1 - CURRENT_DATE)", sql, flags=re.IGNORECASE)
    return sql


def fetch_all(sql, params=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(postgres_sql(sql), params or {})
            return [dict(row) for row in cur.fetchall()] if cur.description else []


def fetch_one(sql, params=None):
    rows = fetch_all(sql, params)
    return rows[0] if rows else None


def execute(sql, params=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(postgres_sql(sql), params or {})


def clean_error(exc):
    msg = str(exc)
    m = re.search(r"((?:ORA-\d{5}|SQLSTATE [0-9A-Z]{5}):.*?)(?:\n|$)", msg)
    return m.group(1) if m else msg.splitlines()[0]

def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()



def survey_data(form):
    errors = []
    try:
        site_id = int(form.get("site_id", ""))
    except ValueError:
        site_id = None
        errors.append("Select a valid site.")

    survey_date_raw = form.get("survey_date", "").strip()
    technician_name = form.get("technician_name", "").strip()
    roof_condition = form.get("roof_condition", "").strip()
    sunlight_condition = form.get("sunlight_condition", "").strip()
    capacity_raw = form.get("recommended_capacity_kw", "").strip()
    remarks = form.get("remarks", "").strip() or None

    survey_date = None
    if not survey_date_raw:
        errors.append("Survey date is required.")
    else:
        try:
            survey_date = parse_date(survey_date_raw)
        except ValueError:
            errors.append("Enter a valid survey date.")

    if not technician_name:
        errors.append("Technician name is required.")
    if not roof_condition:
        errors.append("Roof condition is required.")
    if not sunlight_condition:
        errors.append("Sunlight condition is required.")

    capacity = None
    if not capacity_raw:
        errors.append("Recommended capacity is required.")
    else:
        try:
            capacity = float(capacity_raw)
            if capacity <= 0:
                errors.append("Recommended capacity must be greater than 0.")
        except ValueError:
            errors.append("Recommended capacity must be numeric.")

    return {
        "site_id": site_id,
        "survey_date": survey_date,
        "technician_name": technician_name,
        "roof_condition": roof_condition,
        "sunlight_condition": sunlight_condition,
        "recommended_capacity_kw": capacity,
        "remarks": remarks,
    }, errors

def customer_data(form):
    errors = []
    name = form.get("customer_name", "").strip()
    phone = form.get("phone", "").strip()
    email = form.get("email", "").strip() or None
    address = form.get("address", "").strip() or None

    if not name:
        errors.append("Customer name is required.")
    if not phone:
        errors.append("Phone is required.")
    elif not re.fullmatch(r"\d{10,15}", phone):
        errors.append("Phone must contain 10 to 15 digits.")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        errors.append("Enter a valid email address.")

    return {
        "customer_name": name,
        "phone": phone,
        "email": email,
        "address": address,
    }, errors

def site_data(form):
    errors = []
    try:
        customer_id = int(form.get("customer_id", ""))
    except ValueError:
        customer_id = None
        errors.append("Select a valid customer.")

    site_name = form.get("site_name", "").strip()
    site_address = form.get("site_address", "").strip()
    city = form.get("city", "").strip()
    pincode = form.get("pincode", "").strip() or None
    roof_raw = form.get("roof_area_sq_m", "").strip()

    if not site_name:
        errors.append("Site name is required.")
    if not site_address:
        errors.append("Site address is required.")
    if not city:
        errors.append("City is required.")
    roof = None
    if roof_raw:
        try:
            roof = float(roof_raw)
            if roof <= 0:
                errors.append("Roof area must be greater than 0.")
        except ValueError:
            errors.append("Roof area must be numeric.")
    if pincode and not re.fullmatch(r"\d{4,10}", pincode):
        errors.append("Pincode must contain 4 to 10 digits.")

    return {
        "customer_id": customer_id,
        "site_name": site_name,
        "site_address": site_address,
        "city": city,
        "pincode": pincode,
        "roof_area_sq_m": roof,
    }, errors


@app.route("/")
def dashboard():
    # Catch database failures only. Keep template/rendering errors visible
    # in the terminal so they are not incorrectly labelled as DB failures.
    try:
        counts = {
            "customers": fetch_one("SELECT COUNT(*) n FROM customers")["n"],
            "sites": fetch_one("SELECT COUNT(*) n FROM sites")["n"],
            "surveys": fetch_one("SELECT COUNT(*) n FROM surveys")["n"],
            "installations": fetch_one("SELECT COUNT(*) n FROM installations")["n"],
            "technicians": fetch_one("SELECT COUNT(*) n FROM technicians")["n"],
            "service_requests": fetch_one(
                "SELECT COUNT(*) n FROM service_requests "
                "WHERE request_status IN ('OPEN','ASSIGNED','IN_PROGRESS')"
            )["n"],
        }

        recent = fetch_all("""
            SELECT i.installation_id, c.customer_name, s.site_name,
                   i.scheduled_date, i.installation_status
            FROM installations i
            JOIN sites s ON s.site_id = i.site_id
            JOIN customers c ON c.customer_id = s.customer_id
            ORDER BY i.scheduled_date DESC
            FETCH FIRST 8 ROWS ONLY
        """)
    except Exception:
        app.logger.exception("Dashboard database query failed")
        return (
            "SolarFlow database query failed. Check the terminal for the exact Oracle error.",
            503,
        )

    return render_template("dashboard.html", counts=counts, recent=recent)


@app.route("/customers", methods=["GET", "POST"])
def customers():
    """List customers and create new customer records."""
    if request.method == "POST":
        data, errors = customer_data(request.form)

        if not errors:
            duplicate = fetch_one("""
                SELECT customer_id
                FROM customers
                WHERE phone = :phone
                   OR (:email IS NOT NULL AND email = :email)
                FETCH FIRST 1 ROW ONLY
            """, {"phone": data["phone"], "email": data["email"]})
            if duplicate:
                errors.append("Another customer already uses this phone or email.")

        if errors:
            for error in errors:
                flash(error, "danger")
        else:
            try:
                execute("""
                    INSERT INTO customers (customer_name, phone, email, address)
                    VALUES (:customer_name, :phone, :email, :address)
                """, data)
                flash("Customer added successfully.", "success")
                return redirect(url_for("customers"))
            except Exception as exc:
                flash(clean_error(exc), "danger")

    q = request.args.get("q", "").strip()
    params = {"q": f"%{q}%"} if q else {}
    where = """WHERE LOWER(customer_name) LIKE LOWER(:q)
                  OR phone LIKE :q
                  OR LOWER(email) LIKE LOWER(:q)""" if q else ""
    rows = fetch_all(f"""
        SELECT customer_id, customer_name, phone, email, address
        FROM customers
        {where}
        ORDER BY customer_id DESC
    """, params)
    return render_template("customers.html", rows=rows, q=q)


@app.route("/customers/<int:customer_id>/edit", methods=["GET", "POST"])
def edit_customer(customer_id):
    row = fetch_one("SELECT * FROM customers WHERE customer_id=:id", {"id": customer_id})
    if not row:
        flash("Customer not found.", "danger")
        return redirect(url_for("customers"))

    if request.method == "POST":
        data, errors = customer_data(request.form)
        if not errors:
            dup = fetch_one("""
                SELECT customer_id FROM customers
                WHERE (phone=:phone OR (:email IS NOT NULL AND email=:email))
                  AND customer_id <> :id
            """, {"phone": data["phone"], "email": data["email"], "id": customer_id})
            if dup:
                errors.append("Another customer already uses this phone or email.")
        if errors:
            for e in errors:
                flash(e, "danger")
            row.update(data)
        else:
            try:
                execute("""
                    UPDATE customers
                    SET customer_name=:customer_name,
                        phone=:phone,
                        email=:email,
                        address=:address
                    WHERE customer_id=:id
                """, {**data, "id": customer_id})
                flash("Customer updated successfully.", "success")
                return redirect(url_for("customers"))
            except Exception as exc:
                flash(clean_error(exc), "danger")
                row.update(data)

    return render_template("customer_edit.html", row=row)

@app.post("/customers/<int:customer_id>/delete")
def delete_customer(customer_id):
    try:
        if fetch_one("SELECT site_id FROM sites WHERE customer_id=:id FETCH FIRST 1 ROW ONLY", {"id": customer_id}):
            flash("Customer cannot be deleted because sites are linked to this customer. Delete the linked sites first, or keep the customer record.", "warning")
        else:
            execute("DELETE FROM customers WHERE customer_id=:id", {"id": customer_id})
            flash("Customer deleted successfully.", "success")
    except Exception as exc:
        flash(clean_error(exc), "danger")
    return redirect(url_for("customers"))



@app.route("/surveys", methods=["GET", "POST"])
def surveys():
    if request.method == "POST":
        data, errors = survey_data(request.form)
        if not errors and not fetch_one("SELECT site_id FROM sites WHERE site_id=:id", {"id": data["site_id"]}):
            errors.append("Selected site does not exist.")
        if errors:
            for e in errors:
                flash(e, "danger")
        else:
            try:
                execute("""
                    INSERT INTO surveys(
                        site_id, survey_date, technician_name, roof_condition,
                        sunlight_condition, recommended_capacity_kw, survey_status, remarks
                    )
                    VALUES (
                        :site_id, :survey_date, :technician_name, :roof_condition,
                        :sunlight_condition, :recommended_capacity_kw, 'PENDING', :remarks
                    )
                """, data)
                flash("Survey created successfully and is awaiting approval.", "success")
                return redirect(url_for("surveys"))
            except Exception as exc:
                flash(clean_error(exc), "danger")

    q = request.args.get("q", "").strip()
    params = {"q": f"%{q}%"} if q else {}
    where = """
        WHERE LOWER(s.site_name) LIKE LOWER(:q)
           OR LOWER(c.customer_name) LIKE LOWER(:q)
           OR LOWER(sv.technician_name) LIKE LOWER(:q)
           OR LOWER(sv.survey_status) LIKE LOWER(:q)
    """ if q else ""
    rows = fetch_all(f"""
        SELECT sv.survey_id, sv.site_id, sv.survey_date, sv.technician_name,
               sv.roof_condition, sv.sunlight_condition, sv.recommended_capacity_kw,
               sv.survey_status, sv.remarks, sv.approved_at,
               s.site_name, c.customer_name
        FROM surveys sv
        JOIN sites s ON s.site_id=sv.site_id
        JOIN customers c ON c.customer_id=s.customer_id
        {where}
        ORDER BY sv.survey_id DESC
    """, params)
    site_rows = fetch_all("""
        SELECT s.site_id, s.site_name, c.customer_name
        FROM sites s JOIN customers c ON c.customer_id=s.customer_id
        ORDER BY s.site_name
    """)
    return render_template("surveys.html", rows=rows, sites=site_rows, q=q)


@app.post("/surveys/<int:survey_id>/approve")
def approve_survey(survey_id):
    try:
        row = fetch_one("SELECT survey_status FROM surveys WHERE survey_id=:id", {"id": survey_id})
        if not row:
            flash("Survey not found.", "danger")
        elif row["survey_status"] != "PENDING":
            flash("Only PENDING surveys can be approved.", "warning")
        else:
            execute("""
                UPDATE surveys
                SET survey_status='APPROVED', approved_at=SYSTIMESTAMP
                WHERE survey_id=:id AND survey_status='PENDING'
            """, {"id": survey_id})
            flash("Survey approved successfully.", "success")
    except Exception as exc:
        flash(clean_error(exc), "danger")
    return redirect(url_for("surveys"))


@app.post("/surveys/<int:survey_id>/reject")
def reject_survey(survey_id):
    try:
        row = fetch_one("SELECT survey_status FROM surveys WHERE survey_id=:id", {"id": survey_id})
        if not row:
            flash("Survey not found.", "danger")
        elif row["survey_status"] != "PENDING":
            flash("Only PENDING surveys can be rejected.", "warning")
        else:
            execute("""
                UPDATE surveys
                SET survey_status='REJECTED', approved_at=NULL
                WHERE survey_id=:id AND survey_status='PENDING'
            """, {"id": survey_id})
            flash("Survey rejected.", "success")
    except Exception as exc:
        flash(clean_error(exc), "danger")
    return redirect(url_for("surveys"))


@app.route("/sites", methods=["GET", "POST"])
def sites():
    if request.method == "POST":
        data, errors = site_data(request.form)
        if not errors and not fetch_one("SELECT customer_id FROM customers WHERE customer_id=:id", {"id": data["customer_id"]}):
            errors.append("Selected customer does not exist.")
        if errors:
            for e in errors:
                flash(e, "danger")
        else:
            try:
                execute("""
                    INSERT INTO sites(customer_id, site_name, site_address, city, pincode, roof_area_sq_m)
                    VALUES (:customer_id, :site_name, :site_address, :city, :pincode, :roof_area_sq_m)
                """, data)
                flash("Site registered successfully.", "success")
                return redirect(url_for("sites"))
            except Exception as exc:
                flash(clean_error(exc), "danger")

    q = request.args.get("q", "").strip()
    params = {"q": f"%{q}%"} if q else {}
    where = """
        WHERE LOWER(s.site_name) LIKE LOWER(:q)
           OR LOWER(s.city) LIKE LOWER(:q)
           OR LOWER(c.customer_name) LIKE LOWER(:q)
    """ if q else ""
    rows = fetch_all(f"""
        SELECT s.site_id, s.site_name, s.site_address, s.city, s.pincode,
               s.roof_area_sq_m, s.site_status, s.customer_id,
               c.customer_name
        FROM sites s JOIN customers c ON c.customer_id=s.customer_id
        {where}
        ORDER BY s.site_id DESC
    """, params)
    customer_rows = fetch_all("SELECT customer_id, customer_name FROM customers ORDER BY customer_name")
    return render_template("sites.html", rows=rows, customers=customer_rows, q=q)

@app.route("/sites/<int:site_id>/edit", methods=["GET", "POST"])
def edit_site(site_id):
    row = fetch_one("SELECT * FROM sites WHERE site_id=:id", {"id": site_id})
    if not row:
        flash("Site not found.", "danger")
        return redirect(url_for("sites"))
    if request.method == "POST":
        data, errors = site_data(request.form)
        if not errors and not fetch_one("SELECT customer_id FROM customers WHERE customer_id=:id", {"id": data["customer_id"]}):
            errors.append("Selected customer does not exist.")
        if errors:
            for e in errors:
                flash(e, "danger")
            row.update(data)
        else:
            try:
                execute("""
                    UPDATE sites SET customer_id=:customer_id, site_name=:site_name,
                    site_address=:site_address, city=:city, pincode=:pincode,
                    roof_area_sq_m=:roof_area_sq_m
                    WHERE site_id=:id
                """, {**data, "id": site_id})
                flash("Site updated successfully.", "success")
                return redirect(url_for("sites"))
            except Exception as exc:
                flash(clean_error(exc), "danger")
                row.update(data)
    customers = fetch_all("SELECT customer_id, customer_name FROM customers ORDER BY customer_name")
    return render_template("site_edit.html", row=row, customers=customers)

@app.post("/sites/<int:site_id>/delete")
def delete_site(site_id):
    try:
        execute("DELETE FROM sites WHERE site_id=:id", {"id": site_id})
        flash("Site deleted successfully.", "success")
    except Exception as exc:
        flash(clean_error(exc), "danger")
    return redirect(url_for("sites"))

@app.route("/health")
def health():
    try:
        r = fetch_one("SELECT 1 AS ok FROM dual")
        return {"status": "ok", "oracle": bool(r and r["ok"] == 1)}
    except Exception as exc:
        return {"status": "error", "message": clean_error(exc)}, 500

def design_data(form):
    errors = []
    try:
        survey_id = int(form.get("survey_id", ""))
    except ValueError:
        survey_id = None
        errors.append("Select a valid approved survey.")

    design_date_raw = form.get("design_date", "").strip()
    capacity_raw = form.get("system_capacity_kw", "").strip()
    panels_raw = form.get("panel_count", "").strip()
    inverter_raw = form.get("inverter_capacity_kw", "").strip()
    cost_raw = form.get("estimated_cost", "").strip()
    remarks = form.get("remarks", "").strip() or None

    design_date = None
    if not design_date_raw:
        errors.append("Design date is required.")
    else:
        try:
            design_date = parse_date(design_date_raw)
        except ValueError:
            errors.append("Enter a valid design date.")

    capacity = None
    try:
        capacity = float(capacity_raw)
        if capacity <= 0:
            errors.append("System capacity must be greater than 0.")
    except ValueError:
        errors.append("System capacity must be numeric and greater than 0.")

    panel_count = None
    try:
        panel_count = int(panels_raw)
        if panel_count <= 0:
            errors.append("Panel count must be greater than 0.")
    except ValueError:
        errors.append("Panel count must be a positive whole number.")

    inverter = None
    try:
        inverter = float(inverter_raw)
        if inverter <= 0:
            errors.append("Inverter capacity must be greater than 0.")
    except ValueError:
        errors.append("Inverter capacity must be numeric and greater than 0.")

    cost = None
    try:
        cost = float(cost_raw)
        if cost < 0:
            errors.append("Estimated cost cannot be negative.")
    except ValueError:
        errors.append("Estimated cost must be numeric and non-negative.")

    return {
        "survey_id": survey_id,
        "design_date": design_date,
        "system_capacity_kw": capacity,
        "panel_count": panel_count,
        "inverter_capacity_kw": inverter,
        "estimated_cost": cost,
        "remarks": remarks,
    }, errors




def installation_data(form):
    errors = []
    try:
        design_id = int(form.get("design_id", ""))
    except ValueError:
        design_id = None
        errors.append("Select a valid approved design.")

    scheduled_raw = form.get("scheduled_date", "").strip()
    capacity_raw = form.get("total_capacity_kw", "").strip()
    cost_raw = form.get("installation_cost", "").strip()

    scheduled_date = None
    if not scheduled_raw:
        errors.append("Scheduled date is required.")
    else:
        try:
            scheduled_date = parse_date(scheduled_raw)
        except ValueError:
            errors.append("Enter a valid scheduled date.")

    capacity = None
    try:
        capacity = float(capacity_raw)
        if capacity <= 0:
            errors.append("Total capacity must be greater than 0.")
    except ValueError:
        errors.append("Total capacity must be numeric and greater than 0.")

    cost = None
    try:
        cost = float(cost_raw)
        if cost < 0:
            errors.append("Installation cost cannot be negative.")
    except ValueError:
        errors.append("Installation cost must be numeric and non-negative.")

    return {
        "design_id": design_id,
        "scheduled_date": scheduled_date,
        "total_capacity_kw": capacity,
        "installation_cost": cost,
    }, errors

@app.route("/designs", methods=["GET", "POST"])
def designs():
    if request.method == "POST":
        data, errors = design_data(request.form)
        if not errors:
            survey = fetch_one("""
                SELECT survey_id, recommended_capacity_kw, survey_status
                FROM surveys WHERE survey_id=:id
            """, {"id": data["survey_id"]})
            if not survey:
                errors.append("Selected survey does not exist.")
            elif survey["survey_status"] != "APPROVED":
                errors.append("Design can only be created for an APPROVED survey.")
        if errors:
            for e in errors:
                flash(e, "danger")
        else:
            try:
                execute("""
                    INSERT INTO designs(
                        survey_id, design_date, system_capacity_kw, panel_count,
                        inverter_capacity_kw, design_status, estimated_cost, remarks
                    ) VALUES (
                        :survey_id, :design_date, :system_capacity_kw, :panel_count,
                        :inverter_capacity_kw, 'DRAFT', :estimated_cost, :remarks
                    )
                """, data)
                flash("Design created successfully and is in DRAFT status.", "success")
                return redirect(url_for("designs"))
            except Exception as exc:
                flash(clean_error(exc), "danger")

    q = request.args.get("q", "").strip()
    params = {"q": f"%{q}%"} if q else {}
    where = """WHERE LOWER(s.site_name) LIKE LOWER(:q)
                  OR LOWER(c.customer_name) LIKE LOWER(:q)
                  OR LOWER(d.design_status) LIKE LOWER(:q)""" if q else ""
    rows = fetch_all(f"""
        SELECT d.design_id, d.survey_id, d.design_date, d.system_capacity_kw,
               d.panel_count, d.inverter_capacity_kw, d.design_status,
               d.estimated_cost, d.remarks,
               s.site_name, c.customer_name
        FROM designs d
        JOIN surveys sv ON sv.survey_id=d.survey_id
        JOIN sites s ON s.site_id=sv.site_id
        JOIN customers c ON c.customer_id=s.customer_id
        {where}
        ORDER BY d.design_id DESC
    """, params)
    approved_surveys = fetch_all("""
        SELECT sv.survey_id, sv.survey_date, sv.recommended_capacity_kw,
               s.site_name, c.customer_name
        FROM surveys sv
        JOIN sites s ON s.site_id=sv.site_id
        JOIN customers c ON c.customer_id=s.customer_id
        WHERE sv.survey_status='APPROVED'
        ORDER BY sv.survey_id DESC
    """)
    return render_template("designs.html", rows=rows, surveys=approved_surveys, q=q)


@app.post("/designs/<int:design_id>/approve")
def approve_design(design_id):
    try:
        row = fetch_one("SELECT design_status FROM designs WHERE design_id=:id", {"id": design_id})
        if not row:
            flash("Design not found.", "danger")
        elif row["design_status"] != "DRAFT":
            flash("Only DRAFT designs can be approved.", "warning")
        else:
            execute("""
                UPDATE designs SET design_status='APPROVED'
                WHERE design_id=:id AND design_status='DRAFT'
            """, {"id": design_id})
            flash("Design approved successfully.", "success")
    except Exception as exc:
        flash(clean_error(exc), "danger")
    return redirect(url_for("designs"))


@app.post("/designs/<int:design_id>/reject")
def reject_design(design_id):
    try:
        row = fetch_one("SELECT design_status FROM designs WHERE design_id=:id", {"id": design_id})
        if not row:
            flash("Design not found.", "danger")
        elif row["design_status"] != "DRAFT":
            flash("Only DRAFT designs can be rejected.", "warning")
        else:
            execute("""
                UPDATE designs SET design_status='REJECTED'
                WHERE design_id=:id AND design_status='DRAFT'
            """, {"id": design_id})
            flash("Design rejected.", "success")
    except Exception as exc:
        flash(clean_error(exc), "danger")
    return redirect(url_for("designs"))


@app.route("/installations", methods=["GET", "POST"])
def installations():
    if request.method == "POST":
        data, errors = installation_data(request.form)
        if not errors:
            design = fetch_one("""
                SELECT d.design_id, d.survey_id, d.design_status,
                       d.system_capacity_kw, sv.site_id, sv.survey_status
                FROM designs d
                JOIN surveys sv ON sv.survey_id=d.survey_id
                WHERE d.design_id=:id
            """, {"id": data["design_id"]})
            if not design:
                errors.append("Selected design does not exist.")
            else:
                if design["design_status"] != "APPROVED":
                    errors.append("Installation can only be scheduled for an APPROVED design.")
                if design["survey_status"] != "APPROVED":
                    errors.append("Installation requires an APPROVED survey.")
                data["site_id"] = design["site_id"]
                if data["total_capacity_kw"] is not None and design["system_capacity_kw"] is not None:
                    if data["total_capacity_kw"] > float(design["system_capacity_kw"]):
                        errors.append("Installation capacity cannot exceed the approved design capacity.")
        if errors:
            for e in errors:
                flash(e, "danger")
        else:
            try:
                execute("""
                    INSERT INTO installations(
                        site_id, survey_id, design_id, scheduled_date,
                        installation_status, total_capacity_kw, installation_cost
                    ) VALUES (
                        :site_id, :survey_id, :design_id, :scheduled_date,
                        'SCHEDULED', :total_capacity_kw, :installation_cost
                    )
                """, {
                    **data,
                    "survey_id": design["survey_id"],
                })
                flash("Installation scheduled successfully and is in SCHEDULED status.", "success")
                return redirect(url_for("installations"))
            except Exception as exc:
                flash(clean_error(exc), "danger")

    q = request.args.get("q", "").strip()
    params = {"q": f"%{q}%"} if q else {}
    where = """WHERE LOWER(s.site_name) LIKE LOWER(:q)
                  OR LOWER(c.customer_name) LIKE LOWER(:q)
                  OR LOWER(i.installation_status) LIKE LOWER(:q)""" if q else ""
    rows = fetch_all(f"""
        SELECT i.installation_id, i.site_id, i.survey_id, i.design_id,
               i.scheduled_date, i.installation_date, i.installation_status,
               i.total_capacity_kw, i.installation_cost,
               s.site_name, c.customer_name
        FROM installations i
        JOIN sites s ON s.site_id=i.site_id
        JOIN customers c ON c.customer_id=s.customer_id
        {where}
        ORDER BY i.installation_id DESC
    """, params)
    installation_designs = fetch_all("""
        SELECT d.design_id, d.survey_id, d.system_capacity_kw,
               d.design_status, sv.survey_status,
               s.site_name, c.customer_name
        FROM designs d
        JOIN surveys sv ON sv.survey_id=d.survey_id
        JOIN sites s ON s.site_id=sv.site_id
        JOIN customers c ON c.customer_id=s.customer_id
        ORDER BY d.design_id DESC
    """)
    return render_template("installations.html", rows=rows, designs=installation_designs, q=q)


@app.route("/equipment", methods=["GET", "POST"])
def equipment():
    if request.method == "POST":
        action = request.form.get("action", "")
        try:
            if action == "register_unit":
                model_id = int(request.form.get("model_id", ""))
                serial = request.form.get("serial_number", "").strip()
                purchase_raw = request.form.get("purchase_date", "").strip()
                status = request.form.get("equipment_status", "AVAILABLE").strip().upper()
                errors = []
                if not serial: errors.append("Serial number is required.")
                if status not in {"AVAILABLE", "ASSIGNED", "INSTALLED", "FAULTY", "RETIRED"}: errors.append("Invalid equipment status.")
                purchase_date = None
                if purchase_raw:
                    try: purchase_date = parse_date(purchase_raw)
                    except ValueError: errors.append("Enter a valid purchase date.")
                if fetch_one("SELECT model_id FROM equipment_models WHERE model_id=:id", {"id": model_id}) is None: errors.append("Selected equipment model does not exist.")
                if fetch_one("SELECT equipment_id FROM equipment_units WHERE serial_number=:serial", {"serial": serial}): errors.append("An equipment unit with this serial number already exists.")
                if errors:
                    for e in errors: flash(e, "danger")
                else:
                    execute("INSERT INTO equipment_units(model_id, serial_number, purchase_date, equipment_status) VALUES (:model_id, :serial_number, :purchase_date, :equipment_status)", {"model_id": model_id, "serial_number": serial, "purchase_date": purchase_date, "equipment_status": status})
                    flash("Equipment unit registered successfully.", "success")
                    return redirect(url_for("equipment"))
            elif action == "assign_unit":
                installation_id = int(request.form.get("installation_id", "")); equipment_id = int(request.form.get("equipment_id", "")); quantity = int(request.form.get("quantity", "1")); errors=[]
                if quantity <= 0: errors.append("Quantity must be greater than 0.")
                installation=fetch_one("SELECT installation_id, installation_status FROM installations WHERE installation_id=:id", {"id":installation_id})
                unit=fetch_one("SELECT equipment_id, equipment_status FROM equipment_units WHERE equipment_id=:id", {"id":equipment_id})
                if not installation: errors.append("Selected installation does not exist.")
                elif installation["installation_status"] == "CANCELLED": errors.append("Equipment cannot be assigned to a cancelled installation.")
                if not unit: errors.append("Selected equipment unit does not exist.")
                elif unit["equipment_status"] != "AVAILABLE": errors.append("Only AVAILABLE equipment units can be assigned.")
                if fetch_one("SELECT installation_id FROM installation_equipment WHERE installation_id=:iid AND equipment_id=:eid", {"iid":installation_id,"eid":equipment_id}): errors.append("This equipment unit is already assigned to the selected installation.")
                if errors:
                    for e in errors: flash(e,"danger")
                else:
                    execute("INSERT INTO installation_equipment(installation_id, equipment_id, quantity) VALUES (:installation_id, :equipment_id, :quantity)", {"installation_id":installation_id,"equipment_id":equipment_id,"quantity":quantity})
                    execute("UPDATE equipment_units SET equipment_status='ASSIGNED' WHERE equipment_id=:id", {"id":equipment_id})
                    flash("Equipment assigned to installation successfully.","success")
                    return redirect(url_for("equipment"))
            else: flash("Invalid equipment action.", "danger")
        except ValueError: flash("Enter valid numeric values.", "danger")
        except Exception as exc: flash(clean_error(exc), "danger")
    q=request.args.get("q","").strip(); params={"q":f"%{q}%"} if q else {}
    where="" if not q else "WHERE LOWER(e.serial_number) LIKE LOWER(:q) OR LOWER(em.manufacturer) LIKE LOWER(:q) OR LOWER(em.model_name) LIKE LOWER(:q) OR LOWER(em.equipment_type) LIKE LOWER(:q) OR LOWER(e.equipment_status) LIKE LOWER(:q)"
    units=fetch_all(f"SELECT e.equipment_id,e.serial_number,e.purchase_date,e.equipment_status,em.model_id,em.equipment_type,em.manufacturer,em.model_name,em.rated_capacity_kw,em.warranty_months,em.unit_price FROM equipment_units e JOIN equipment_models em ON em.model_id=e.model_id {where} ORDER BY e.equipment_id DESC",params)
    models=fetch_all("SELECT model_id,equipment_type,manufacturer,model_name,rated_capacity_kw,unit_price,warranty_months FROM equipment_models ORDER BY model_id DESC")
    available_units=fetch_all("SELECT e.equipment_id,e.serial_number,em.equipment_type,em.manufacturer,em.model_name,em.rated_capacity_kw FROM equipment_units e JOIN equipment_models em ON em.model_id=e.model_id WHERE e.equipment_status='AVAILABLE' ORDER BY e.equipment_id DESC")
    installations_list=fetch_all("SELECT i.installation_id,i.installation_status,s.site_name,c.customer_name FROM installations i JOIN sites s ON s.site_id=i.site_id JOIN customers c ON c.customer_id=s.customer_id WHERE i.installation_status <> 'CANCELLED' ORDER BY i.installation_id DESC")
    assignments=fetch_all("SELECT ie.installation_id,ie.equipment_id,ie.quantity,e.serial_number,em.equipment_type,em.manufacturer,em.model_name,s.site_name,c.customer_name FROM installation_equipment ie JOIN equipment_units e ON e.equipment_id=ie.equipment_id JOIN equipment_models em ON em.model_id=e.model_id JOIN installations i ON i.installation_id=ie.installation_id JOIN sites s ON s.site_id=i.site_id JOIN customers c ON c.customer_id=s.customer_id ORDER BY ie.installation_id DESC,ie.equipment_id DESC")
    return render_template("equipment.html",units=units,models=models,available_units=available_units,installations=installations_list,assignments=assignments,q=q)


def update_installation_status(installation_id, new_status):
    allowed = {
        "SCHEDULED": {"IN_PROGRESS", "CANCELLED"},
        "IN_PROGRESS": {"COMPLETED", "CANCELLED"},
        "COMPLETED": set(),
        "CANCELLED": set(),
    }
    row = fetch_one("SELECT installation_status FROM installations WHERE installation_id=:id", {"id": installation_id})
    if not row:
        return "Installation not found.", "danger"
    current = row["installation_status"]
    if new_status not in allowed.get(current, set()):
        return f"Cannot change installation from {current} to {new_status}.", "warning"
    if new_status == "COMPLETED":
        execute("""
            UPDATE installations
            SET installation_status='COMPLETED',
                installation_date=NVL(installation_date, SYSDATE),
                completion_date=NVL(completion_date, SYSDATE)
            WHERE installation_id=:id AND installation_status='IN_PROGRESS'
        """, {"id": installation_id})
    else:
        execute("""
            UPDATE installations
            SET installation_status=:status
            WHERE installation_id=:id
        """, {"id": installation_id, "status": new_status})
    return f"Installation status changed to {new_status}.", "success"


@app.post("/installations/<int:installation_id>/status/<status>")
def change_installation_status(installation_id, status):
    status = status.upper()
    if status not in {"IN_PROGRESS", "COMPLETED", "CANCELLED"}:
        flash("Invalid installation status.", "danger")
    else:
        try:
            message, category = update_installation_status(installation_id, status)
            flash(message, category)
        except Exception as exc:
            flash(clean_error(exc), "danger")
    return redirect(url_for("installations"))


@app.route("/technician-assignments", methods=["GET", "POST"])
def technician_assignments():
    if request.method == "POST":
        try:
            installation_id = int(request.form.get("installation_id", ""))
            technician_id = int(request.form.get("technician_id", ""))
            start_raw = request.form.get("start_datetime", "").strip()
            end_raw = request.form.get("end_datetime", "").strip()
            errors = []
            if not start_raw or not end_raw:
                errors.append("Start date/time and end date/time are required.")
            start_dt = end_dt = None
            try:
                start_dt = datetime.strptime(start_raw, "%Y-%m-%dT%H:%M")
                end_dt = datetime.strptime(end_raw, "%Y-%m-%dT%H:%M")
                if end_dt <= start_dt:
                    errors.append("End date/time must be after start date/time.")
            except ValueError:
                if start_raw or end_raw:
                    errors.append("Enter valid start and end date/time values.")

            installation = fetch_one("SELECT installation_id, installation_status FROM installations WHERE installation_id=:id", {"id": installation_id})
            technician = fetch_one("SELECT technician_id, technician_name, technician_status FROM technicians WHERE technician_id=:id", {"id": technician_id})
            if not installation:
                errors.append("Selected installation does not exist.")
            elif installation["installation_status"] == "CANCELLED":
                errors.append("A technician cannot be assigned to a cancelled installation.")
            if not technician:
                errors.append("Selected technician does not exist.")
            elif technician["technician_status"] != "ACTIVE":
                errors.append("Only ACTIVE technicians can be assigned.")

            if start_dt and end_dt and not errors:
                overlap = fetch_one("""
                    SELECT assignment_id
                    FROM assignments
                    WHERE technician_id=:technician_id
                      AND assignment_status IN ('ASSIGNED','COMPLETED')
                      AND start_datetime < :end_datetime
                      AND NVL(end_datetime, TIMESTAMP '9999-12-31 23:59:59') > :start_datetime
                    FETCH FIRST 1 ROW ONLY
                """, {"technician_id": technician_id, "start_datetime": start_dt, "end_datetime": end_dt})
                if overlap:
                    errors.append("Technician already has an overlapping assignment during this time.")

            if errors:
                for e in errors: flash(e, "danger")
            else:
                execute("""
                    INSERT INTO assignments(installation_id, technician_id, start_datetime, end_datetime, assignment_status)
                    VALUES (:installation_id, :technician_id, :start_datetime, :end_datetime, 'ASSIGNED')
                """, {"installation_id": installation_id, "technician_id": technician_id, "start_datetime": start_dt, "end_datetime": end_dt})
                flash("Technician assigned to installation successfully.", "success")
                return redirect(url_for("technician_assignments"))
        except ValueError:
            flash("Select valid installation and technician values.", "danger")
        except Exception as exc:
            flash(clean_error(exc), "danger")

    technicians = fetch_all("""
        SELECT technician_id, technician_name, phone, email, specialization, hire_date, technician_status AS status
        FROM technicians ORDER BY technician_id DESC
    """)
    installations = fetch_all("""
        SELECT i.installation_id, i.installation_status, i.scheduled_date,
               s.site_name, c.customer_name
        FROM installations i
        JOIN sites s ON s.site_id=i.site_id
        JOIN customers c ON c.customer_id=s.customer_id
        WHERE i.installation_status <> 'CANCELLED'
        ORDER BY i.installation_id DESC
    """)
    assignments = fetch_all("""
        SELECT a.assignment_id, a.installation_id, a.technician_id,
               a.start_datetime, a.end_datetime, a.assignment_status AS status,
               t.technician_name, t.specialization,
               s.site_name, c.customer_name
        FROM assignments a
        JOIN technicians t ON t.technician_id=a.technician_id
        JOIN installations i ON i.installation_id=a.installation_id
        JOIN sites s ON s.site_id=i.site_id
        JOIN customers c ON c.customer_id=s.customer_id
        ORDER BY a.assignment_id DESC
    """)
    return render_template("technician_assignments.html", technicians=technicians, installations=installations, assignments=assignments)

@app.post("/technician-assignments/<int:assignment_id>/status/<status>")
def technician_assignment_status(assignment_id, status):
    status = status.upper()
    if status not in {"COMPLETED", "CANCELLED"}:
        flash("Invalid assignment status.", "danger")
        return redirect(url_for("technician_assignments"))
    try:
        row = fetch_one("SELECT assignment_status AS status FROM assignments WHERE assignment_id=:id", {"id": assignment_id})
        if not row:
            flash("Assignment not found.", "danger")
        elif row["status"] != "ASSIGNED":
            flash(f"Only ASSIGNED records can be changed. Current status: {row['status']}.", "warning")
        else:
            execute("UPDATE assignments SET assignment_status=:status WHERE assignment_id=:id", {"status": status, "id": assignment_id})
            flash(f"Technician assignment marked {status}.", "success")
    except Exception as exc:
        flash(clean_error(exc), "danger")
    return redirect(url_for("technician_assignments"))





# ---------------- Warranty & Service Management ----------------
@app.route('/warranty-service', methods=['GET','POST'])
def warranty_service():
    if request.method == 'POST':
        action = request.form.get('action','').strip()
        try:
            if action == 'warranty':
                equipment_id = int(request.form.get('equipment_id',''))
                start_raw = request.form.get('warranty_start','').strip()
                end_raw = request.form.get('warranty_end','').strip()
                errors=[]
                try:
                    start = parse_date(start_raw); end = parse_date(end_raw)
                    if end < start: errors.append('Warranty end date must be on or after the start date.')
                except ValueError:
                    start=end=None; errors.append('Enter valid warranty dates.')
                equipment = fetch_one('SELECT equipment_id, serial_number FROM equipment_units WHERE equipment_id=:id', {'id':equipment_id})
                if not equipment: errors.append('Selected equipment unit does not exist.')
                existing = fetch_one('SELECT warranty_id FROM warranties WHERE equipment_id=:id', {'id':equipment_id})
                if existing: errors.append('This equipment unit already has a warranty record.')
                if errors:
                    for e in errors: flash(e,'danger')
                else:
                    execute("INSERT INTO warranties(equipment_id,warranty_start,warranty_end) VALUES (:equipment_id,:start_date,:end_date)", {'equipment_id':equipment_id,'start_date':start,'end_date':end})
                    flash('Warranty registered successfully.','success')
                    return redirect(url_for('warranty_service'))
            elif action == 'service_request':
                site_id = int(request.form.get('site_id',''))
                equipment_raw = request.form.get('equipment_id','').strip()
                equipment_id = int(equipment_raw) if equipment_raw else None
                request_date_raw = request.form.get('request_date','').strip()
                issue = request.form.get('issue_description','').strip()
                priority = request.form.get('priority','MEDIUM').upper()
                errors=[]
                try: request_date=parse_date(request_date_raw)
                except ValueError: request_date=None; errors.append('Enter a valid request date.')
                if not issue: errors.append('Issue description is required.')
                if priority not in {'LOW','MEDIUM','HIGH','CRITICAL'}: errors.append('Invalid service priority.')
                site = fetch_one('SELECT site_id FROM sites WHERE site_id=:id', {'id':site_id})
                if not site: errors.append('Selected site does not exist.')
                if not equipment_id:
                    errors.append('Select the equipment unit related to this service request.')
                if equipment_id:
                    eq = fetch_one('''SELECT e.equipment_id, e.serial_number, i.site_id
                                      FROM equipment_units e
                                      LEFT JOIN installation_equipment ie ON ie.equipment_id=e.equipment_id
                                      LEFT JOIN installations i ON i.installation_id=ie.installation_id
                                      WHERE e.equipment_id=:id
                                      ORDER BY i.installation_id DESC FETCH FIRST 1 ROW ONLY''', {'id':equipment_id})
                    if not eq: errors.append('Selected equipment unit does not exist.')
                    elif eq['site_id'] is not None and eq['site_id'] != site_id: errors.append('Selected equipment is assigned to a different site.')
                if errors:
                    for e in errors: flash(e,'danger')
                else:
                    execute('''INSERT INTO service_requests(site_id,equipment_id,request_date,issue_description,priority)
                               VALUES (:site_id,:equipment_id,:request_date,:issue_description,:priority)''',
                            {'site_id':site_id,'equipment_id':equipment_id,'request_date':request_date,'issue_description':issue,'priority':priority})
                    flash('Service request created successfully.','success')
                    return redirect(url_for('warranty_service'))
            elif action == 'visit':
                service_request_id = int(request.form.get('service_request_id',''))
                technician_id = int(request.form.get('technician_id',''))
                visit_date_raw = request.form.get('visit_date','').strip()
                errors=[]
                try: visit_date=parse_date(visit_date_raw)
                except ValueError: visit_date=None; errors.append('Enter a valid visit date.')
                req = fetch_one("SELECT service_request_id, request_status FROM service_requests WHERE service_request_id=:id", {'id':service_request_id})
                tech = fetch_one("SELECT technician_id, technician_status FROM technicians WHERE technician_id=:id", {'id':technician_id})
                if not req: errors.append('Selected service request does not exist.')
                elif req['request_status'] in {'CLOSED','CANCELLED','RESOLVED'}: errors.append('A visit cannot be scheduled for a closed, cancelled, or resolved request.')
                if not tech: errors.append('Selected technician does not exist.')
                elif tech['technician_status'] != 'ACTIVE': errors.append('Only ACTIVE technicians can be assigned to service visits.')
                if errors:
                    for e in errors: flash(e,'danger')
                else:
                    execute('''INSERT INTO visits(service_request_id,technician_id,visit_date)
                               VALUES (:service_request_id,:technician_id,:visit_date)''', {'service_request_id':service_request_id,'technician_id':technician_id,'visit_date':visit_date})
                    execute("UPDATE service_requests SET request_status='ASSIGNED' WHERE service_request_id=:id AND request_status='OPEN'", {'id':service_request_id})
                    flash('Service visit scheduled successfully.','success')
                    return redirect(url_for('warranty_service'))
            elif action == 'request_status':
                request_id=int(request.form.get('service_request_id',''))
                status=request.form.get('status','').upper()
                if status not in {'IN_PROGRESS','RESOLVED','CLOSED','CANCELLED'}: flash('Invalid service request status.','danger')
                else:
                    row=fetch_one('SELECT request_status FROM service_requests WHERE service_request_id=:id', {'id':request_id})
                    if not row: flash('Service request not found.','danger')
                    elif row['request_status'] in {'RESOLVED','CLOSED','CANCELLED'}:
                        flash('A resolved, closed, or cancelled service request cannot be changed.','warning')
                    elif status == 'CANCELLED' and row['request_status'] not in {'OPEN','ASSIGNED','IN_PROGRESS'}:
                        flash('Only OPEN, ASSIGNED, or IN_PROGRESS requests can be cancelled.','warning')
                    else:
                        execute('UPDATE service_requests SET request_status=:status WHERE service_request_id=:id', {'status':status,'id':request_id})
                        flash(f'Service request marked {status}.','success')
                return redirect(url_for('warranty_service'))
            elif action == 'visit_status':
                visit_id=int(request.form.get('visit_id',''))
                status=request.form.get('status','').upper()
                if status not in {'COMPLETED','CANCELLED'}: flash('Invalid visit status.','danger')
                else:
                    row=fetch_one('SELECT visit_status FROM visits WHERE visit_id=:id', {'id':visit_id})
                    if not row: flash('Visit not found.','danger')
                    elif row['visit_status'] != 'SCHEDULED': flash('Only SCHEDULED visits can be changed.','warning')
                    else:
                        execute('UPDATE visits SET visit_status=:status WHERE visit_id=:id', {'status':status,'id':visit_id})
                        flash(f'Visit marked {status}.','success')
                return redirect(url_for('warranty_service'))
        except (ValueError, TypeError):
            flash('Select valid values for all required fields.','danger')
        except Exception as exc:
            flash(clean_error(exc),'danger')

    equipment = fetch_all('''SELECT e.equipment_id,e.serial_number,e.equipment_status,em.manufacturer,em.model_name
                             FROM equipment_units e JOIN equipment_models em ON em.model_id=e.model_id
                             ORDER BY e.equipment_id DESC''')
    warranty_equipment = fetch_all('''SELECT e.equipment_id,e.serial_number,e.equipment_status,em.manufacturer,em.model_name
                                     FROM equipment_units e
                                     JOIN equipment_models em ON em.model_id=e.model_id
                                     LEFT JOIN warranties w ON w.equipment_id=e.equipment_id
                                     WHERE w.warranty_id IS NULL
                                     ORDER BY e.equipment_id DESC''')
    available_equipment = fetch_all('''SELECT e.equipment_id,e.serial_number,em.manufacturer,em.model_name,
                                              i.site_id
                                       FROM equipment_units e
                                       JOIN equipment_models em ON em.model_id=e.model_id
                                       LEFT JOIN installation_equipment ie ON ie.equipment_id=e.equipment_id
                                       LEFT JOIN installations i ON i.installation_id=ie.installation_id
                                       WHERE e.equipment_status IN ('ASSIGNED','INSTALLED')
                                       ORDER BY e.equipment_id DESC''')
    sites = fetch_all('SELECT site_id,site_name,customer_id FROM sites ORDER BY site_id DESC')
    site_customers = fetch_all('''SELECT s.site_id,s.site_name,c.customer_name FROM sites s JOIN customers c ON c.customer_id=s.customer_id ORDER BY s.site_id DESC''')
    technicians = fetch_all("SELECT technician_id,technician_name,technician_status FROM technicians WHERE technician_status='ACTIVE' ORDER BY technician_id DESC")
    warranties = fetch_all('''SELECT w.warranty_id,w.equipment_id,w.warranty_start,w.warranty_end,w.warranty_status,e.serial_number,em.manufacturer,em.model_name
                              FROM warranties w JOIN equipment_units e ON e.equipment_id=w.equipment_id
                              JOIN equipment_models em ON em.model_id=e.model_id ORDER BY w.warranty_id DESC''')
    requests_rows = fetch_all('''SELECT r.service_request_id,r.request_date,r.issue_description,r.priority,r.request_status,
                                       s.site_name,c.customer_name,e.serial_number
                                FROM service_requests r JOIN sites s ON s.site_id=r.site_id
                                JOIN customers c ON c.customer_id=s.customer_id
                                LEFT JOIN equipment_units e ON e.equipment_id=r.equipment_id
                                ORDER BY r.service_request_id DESC''')
    open_requests = fetch_all("SELECT service_request_id,site_id,issue_description,request_status FROM service_requests WHERE request_status IN ('OPEN','ASSIGNED','IN_PROGRESS') ORDER BY service_request_id DESC")
    visits = fetch_all('''SELECT v.visit_id,v.visit_date,v.visit_status,v.work_performed,v.service_charge,
                                 r.service_request_id,r.issue_description,t.technician_name,s.site_name
                          FROM visits v JOIN service_requests r ON r.service_request_id=v.service_request_id
                          JOIN technicians t ON t.technician_id=v.technician_id
                          JOIN sites s ON s.site_id=r.site_id ORDER BY v.visit_id DESC''')
    return render_template('warranty_service.html', equipment=equipment, available_equipment=available_equipment,
                           sites=sites, site_customers=site_customers, technicians=technicians,
                           warranties=warranties, warranty_equipment=warranty_equipment, requests=requests_rows, open_requests=open_requests, visits=visits)

# ---------------- Phase 8: Generation / Energy Management ----------------
@app.route('/generation', methods=['GET','POST'])
def generation():
    if request.method == 'POST':
        try:
            site_id = int(request.form.get('site_id',''))
            reading_date = parse_date(request.form.get('reading_date',''))
            energy = float(request.form.get('energy_generated_kwh',''))
            peak_raw = request.form.get('peak_power_kw','').strip()
            meter_raw = request.form.get('meter_reading_kwh','').strip()
            peak = float(peak_raw) if peak_raw else None
            meter = float(meter_raw) if meter_raw else None
            notes = request.form.get('notes','').strip() or None
            errors=[]
            if energy < 0: errors.append('Energy generated must be non-negative.')
            if peak is not None and peak < 0: errors.append('Peak power must be non-negative.')
            if meter is not None and meter < 0: errors.append('Meter reading must be non-negative.')
            if not fetch_one('SELECT site_id FROM sites WHERE site_id=:id', {'id':site_id}): errors.append('Selected site does not exist.')
            if fetch_one('SELECT reading_id FROM generation_readings WHERE site_id=:site_id AND reading_date=:reading_date', {'site_id':site_id,'reading_date':reading_date}): errors.append('A generation reading already exists for this site and date.')
            if errors:
                for e in errors: flash(e,'danger')
            else:
                execute('INSERT INTO generation_readings(site_id,reading_date,energy_generated_kwh,peak_power_kw,meter_reading_kwh,notes) VALUES (:site_id,:reading_date,:energy,:peak,:meter,:notes)', {'site_id':site_id,'reading_date':reading_date,'energy':energy,'peak':peak,'meter':meter,'notes':notes})
                flash('Generation reading recorded successfully.','success')
                return redirect(url_for('generation'))
        except (ValueError, TypeError): flash('Enter valid values for the generation reading.','danger')
        except Exception as exc: flash(clean_error(exc),'danger')
    sites=fetch_all('SELECT s.site_id,s.site_name,c.customer_name FROM sites s JOIN customers c ON c.customer_id=s.customer_id ORDER BY s.site_id DESC')
    rows=fetch_all('SELECT g.reading_id,g.reading_date,g.energy_generated_kwh,g.peak_power_kw,g.meter_reading_kwh,g.notes,s.site_name,c.customer_name FROM generation_readings g JOIN sites s ON s.site_id=g.site_id JOIN customers c ON c.customer_id=s.customer_id ORDER BY g.reading_date DESC,g.reading_id DESC')
    return render_template('generation.html', sites=sites, rows=rows)

# ---------------- Phase 9: Billing / Payments ----------------
@app.route('/billing', methods=['GET','POST'])
def billing():
    if request.method == 'POST':
        action=request.form.get('action','')
        try:
            if action=='invoice':
                customer_id=int(request.form.get('customer_id',''))
                installation_raw=request.form.get('installation_id','').strip()
                installation_id=int(installation_raw) if installation_raw else None
                invoice_date=parse_date(request.form.get('invoice_date',''))
                due_date=parse_date(request.form.get('due_date',''))
                amount=float(request.form.get('invoice_amount',''))
                errors=[]
                if due_date < invoice_date: errors.append('Due date must be on or after invoice date.')
                if amount < 0: errors.append('Invoice amount cannot be negative.')
                if not fetch_one('SELECT customer_id FROM customers WHERE customer_id=:id', {'id':customer_id}): errors.append('Selected customer does not exist.')
                if installation_id and not fetch_one('SELECT installation_id FROM installations WHERE installation_id=:id', {'id':installation_id}): errors.append('Selected installation does not exist.')
                if errors:
                    for e in errors: flash(e,'danger')
                else:
                    execute("INSERT INTO invoices(customer_id,installation_id,invoice_date,due_date,invoice_amount,invoice_status) VALUES (:customer_id,:installation_id,:invoice_date,:due_date,:amount,'UNPAID')", {'customer_id':customer_id,'installation_id':installation_id,'invoice_date':invoice_date,'due_date':due_date,'amount':amount})
                    flash('Invoice created successfully.','success'); return redirect(url_for('billing'))
            elif action=='payment':
                invoice_id=int(request.form.get('invoice_id',''))
                payment_date=parse_date(request.form.get('payment_date',''))
                amount=float(request.form.get('amount',''))
                method=request.form.get('payment_method','').upper()
                ref=request.form.get('transaction_reference','').strip() or None
                errors=[]
                inv=fetch_one('SELECT invoice_id,invoice_amount,invoice_status FROM invoices WHERE invoice_id=:id', {'id':invoice_id})
                if not inv: errors.append('Selected invoice does not exist.')
                if amount <= 0: errors.append('Payment amount must be greater than 0.')
                if method not in {'CASH','UPI','CARD','BANK_TRANSFER','CHEQUE'}: errors.append('Select a valid payment method.')
                if ref and fetch_one('SELECT payment_id FROM payments WHERE transaction_reference=:ref', {'ref':ref}): errors.append('Transaction reference already exists.')
                if inv and inv['invoice_status'] == 'CANCELLED': errors.append('Cannot record payment for a cancelled invoice.')
                if errors:
                    for e in errors: flash(e,'danger')
                else:
                    paid=fetch_one("SELECT NVL(SUM(amount),0) paid FROM payments WHERE invoice_id=:id AND payment_status='SUCCESS'", {'id':invoice_id})['paid']
                    if float(paid)+amount > float(inv['invoice_amount']):
                        flash('Payment cannot exceed the invoice balance.','danger')
                    else:
                        execute("INSERT INTO payments(invoice_id,payment_date,amount,payment_method,transaction_reference,payment_status) VALUES (:invoice_id,:payment_date,:amount,:method,:ref,'SUCCESS')", {'invoice_id':invoice_id,'payment_date':payment_date,'amount':amount,'method':method,'ref':ref})
                        new_status='PAID' if abs(float(paid)+amount-float(inv['invoice_amount']))<0.005 else 'PARTIALLY_PAID'
                        execute('UPDATE invoices SET invoice_status=:status WHERE invoice_id=:id', {'status':new_status,'id':invoice_id})
                        flash('Payment recorded successfully.','success'); return redirect(url_for('billing'))
        except (ValueError, TypeError): flash('Enter valid billing values.','danger')
        except Exception as exc: flash(clean_error(exc),'danger')
    customers_rows=fetch_all('SELECT customer_id,customer_name FROM customers ORDER BY customer_name')
    installations_rows=fetch_all('SELECT i.installation_id,s.site_name,c.customer_name,i.installation_status FROM installations i JOIN sites s ON s.site_id=i.site_id JOIN customers c ON c.customer_id=s.customer_id ORDER BY i.installation_id DESC')
    invoices=fetch_all("SELECT i.invoice_id,i.invoice_date,i.due_date,i.invoice_amount,i.invoice_status,c.customer_name,s.site_name,NVL((SELECT SUM(p.amount) FROM payments p WHERE p.invoice_id=i.invoice_id AND p.payment_status='SUCCESS'),0) paid_amount FROM invoices i JOIN customers c ON c.customer_id=i.customer_id LEFT JOIN installations ins ON ins.installation_id=i.installation_id LEFT JOIN sites s ON s.site_id=ins.site_id ORDER BY i.invoice_id DESC")
    unpaid=fetch_all("SELECT i.invoice_id,c.customer_name,i.invoice_amount,i.invoice_status,NVL((SELECT SUM(p.amount) FROM payments p WHERE p.invoice_id=i.invoice_id AND p.payment_status='SUCCESS'),0) paid_amount FROM invoices i JOIN customers c ON c.customer_id=i.customer_id WHERE i.invoice_status IN ('UNPAID','PARTIALLY_PAID') ORDER BY i.invoice_id DESC")
    payments=fetch_all("SELECT p.payment_id,p.payment_date,p.amount,p.payment_method,p.transaction_reference,p.payment_status,p.invoice_id,c.customer_name FROM payments p JOIN invoices i ON i.invoice_id=p.invoice_id JOIN customers c ON c.customer_id=i.customer_id ORDER BY p.payment_id DESC")
    return render_template('billing.html', customers=customers_rows, installations=installations_rows, invoices=invoices, unpaid=unpaid, payments=payments)

# ---------------- Phase 10: Reports ----------------
@app.route('/reports')
def reports():
    installation_report=fetch_all('SELECT i.installation_id,c.customer_name,s.site_name,i.scheduled_date,i.installation_status,i.total_capacity_kw,i.installation_cost FROM installations i JOIN sites s ON s.site_id=i.site_id JOIN customers c ON c.customer_id=s.customer_id ORDER BY i.installation_id DESC')
    warranty_report=fetch_all('SELECT w.warranty_id,e.serial_number,em.manufacturer,em.model_name,w.warranty_start,w.warranty_end,w.warranty_status,(w.warranty_end - CURRENT_DATE) days_remaining FROM warranties w JOIN equipment_units e ON e.equipment_id=w.equipment_id JOIN equipment_models em ON em.model_id=e.model_id ORDER BY w.warranty_end')
    maintenance_report=fetch_all('SELECT r.service_request_id,c.customer_name,s.site_name,e.serial_number,r.request_date,r.priority,r.request_status,r.issue_description FROM service_requests r JOIN sites s ON s.site_id=r.site_id JOIN customers c ON c.customer_id=s.customer_id LEFT JOIN equipment_units e ON e.equipment_id=r.equipment_id ORDER BY r.request_date DESC')
    technician_report=fetch_all('SELECT t.technician_id,t.technician_name,t.specialization,COUNT(a.assignment_id) assignment_count FROM technicians t LEFT JOIN assignments a ON a.technician_id=t.technician_id GROUP BY t.technician_id,t.technician_name,t.specialization ORDER BY assignment_count DESC,t.technician_name')
    generation_report=fetch_all('SELECT s.site_name,c.customer_name,COUNT(g.reading_id) reading_count,NVL(SUM(g.energy_generated_kwh),0) total_kwh,NVL(AVG(g.energy_generated_kwh),0) avg_kwh FROM sites s JOIN customers c ON c.customer_id=s.customer_id LEFT JOIN generation_readings g ON g.site_id=s.site_id GROUP BY s.site_name,c.customer_name ORDER BY total_kwh DESC')
    revenue_report=fetch_all("SELECT c.customer_name,COUNT(i.invoice_id) invoice_count,NVL(SUM(i.invoice_amount),0) invoiced_amount,NVL(SUM((SELECT SUM(p.amount) FROM payments p WHERE p.invoice_id=i.invoice_id AND p.payment_status='SUCCESS')),0) paid_amount FROM customers c LEFT JOIN invoices i ON i.customer_id=c.customer_id GROUP BY c.customer_name ORDER BY invoiced_amount DESC")
    return render_template('reports.html', installation_report=installation_report,warranty_report=warranty_report,maintenance_report=maintenance_report,technician_report=technician_report,generation_report=generation_report,revenue_report=revenue_report)



import os

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False
    )   