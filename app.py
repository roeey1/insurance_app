from google import genai
import json
import streamlit as st
import pandas as pd
from datetime import date
import time
import matplotlib
matplotlib.use('Agg')  # <-- THIS FIXES THE SILENT MAC CRASH
import matplotlib.pyplot as plt
import webbrowser
import os
import tempfile
import io
import base64

# --- Configure Google Gemini ---
client = genai.Client(api_key="AQ.Ab8RN6LlPLaJUM0TJPMO86Fgk1jI4Hcej7uDFNXuszGyDVcW5A")


def extract_policy_members(uploaded_file):
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(uploaded_file.getvalue())
            temp_path = tmp.name

        gemini_file = client.files.upload(file=temp_path)

        prompt = """
        Analyze this Israeli insurance policy PDF. 
        Look at the personal details section ("פרטי המבוטח/ים בפוליסה") where actual birth dates ("תאריך לידה") are listed for each person.

        CRITICAL: Do NOT use the entry age ("גיל כניסה") from the coverage tables. We need their REAL current age based on their birth date for the year 2026.

        For each person, extract:
        - "name" (string: Full name / שם מלא)
        - "id_num" (string, optional: ID number)
        - "birth_date" (string: birth date in DD/MM/YYYY format)
        - "current_age" (integer: the real current age of the person calculated for the year 2026 based on their birth date).

        Return ONLY a valid JSON array of objects with these exact keys: name, id_num, birth_date, current_age.
        """

        response = client.models.generate_content(
            model='gemini-3.6-flash',
            contents=[gemini_file, prompt]
        )

        response_text = response.text.strip()
        clean_json_str = response_text.replace('```json', '').replace('```', '').strip()
        members = json.loads(clean_json_str)
        return members, None
    except Exception as e:
        return [], str(e)
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


def extract_targeted_policy_premiums(uploaded_file, target_name="", target_id="", insurance_type="life"):
    temp_path = None
    max_retries = 3

    if insurance_type == "life":
        target_keyword = "ריסק"
        exclusion_instruction = "Do NOT extract health or critical illness coverages."
    else:
        target_keyword = "מענקית, מזור מורחב, קרן אור טופ, בשביל החוסן, מענקית זהב, מדיכלל מחלות קשות, מרפא, ביטוח מחלות קשות"
        exclusion_instruction = "STRICT EXCLUSION: Do NOT extract life insurance ('ריסק') or general health coverages. Look ONLY for Critical Illness coverage (e.g., 'מענקית', 'מזור מורחב')."

    for attempt in range(max_retries):
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp.write(uploaded_file.getvalue())
                temp_path = tmp.name

            gemini_file = client.files.upload(file=temp_path)

            prompt = f"""
            Analyze this Israeli insurance policy PDF (Harel, Migdal, or Menora format).
            We are looking strictly for the insured person:
            - Name: {target_name}
            - ID: {target_id}
            - Insurance Type Requested: {insurance_type.upper()} ('life' = ריסק, 'ci' = מחלות קשות).
            - Target Keyword: {target_keyword}
            - {exclusion_instruction}

            EXTRACTION INSTRUCTIONS (STEP FUNCTION & DISCOUNTS):
            1. Find the price escalation table ("פירוט השתנות מחיר הביטוח") for the requested coverage (e.g., 'מענקית' or 'קרן אור טופ').
            2. Extract the **FULL price before discount** into "steps" as an array of objects, where each object contains:
               - "start_year" (integer, e.g., 2031, 2036)
               - "monthly_premium" (float)
            3. Look for active discount details (e.g., "אחוז הנחה" like 20%, and "תאריך סיום הנחה"). Extract them into:
               - "discount_percent" (float, e.g., 20.0 if there is a 20% discount, otherwise 0.0)
               - "discount_end_year" (integer, e.g., 2027, or null if no discount)
            4. Find the exact birth date of {target_name} and calculate their real current age for the year 2026 ("client_age").
            5. Extract total coverage amount ("coverage_amount").

            Return ONLY a valid JSON object with these exact keys: client_age, coverage_amount, current_premium, future_premiums, steps, discount_percent, discount_end_year, error.
            """

            response = client.models.generate_content(
                model='gemini-3.6-flash',
                contents=[gemini_file, prompt]
            )

            response_text = response.text.strip()
            clean_json_str = response_text.replace('```json', '').replace('```', '').strip()
            data = json.loads(clean_json_str)

            if data.get("error") or not data.get("coverage_amount") or float(data.get("coverage_amount", 0)) == 0:
                return [], 40, 0, None, f"למבוטח {target_name} אין כיסוי מתאים בפוליסה זו."

            detected_age = int(data.get("client_age", 40))
            detected_coverage = float(data.get("coverage_amount", 250000))
            current_premium = float(data.get("current_premium", 100.0))

            steps = data.get("steps", [])
            discount_percent = float(data.get("discount_percent", 0.0))
            discount_end_year = data.get("discount_end_year")
            if discount_end_year:
                try:
                    discount_end_year = int(discount_end_year)
                except:
                    discount_end_year = None

            max_expiry_age = 75 if insurance_type == "ci" else 80
            total_policy_years = max(1, max_expiry_age - detected_age)
            current_calendar_year = date.today().year

            sorted_steps = sorted(
                [s for s in steps if isinstance(s, dict) and 'start_year' in s and 'monthly_premium' in s],
                key=lambda x: x['start_year']) if isinstance(steps, list) else []

            rates_array = []
            # אתחול מחיר ברירת מחדל התחלי
            current_price = current_premium
            if len(sorted_steps) > 0 and int(sorted_steps[0]['start_year']) <= current_calendar_year:
                current_price = float(sorted_steps[0]['monthly_premium'])

            for i in range(total_policy_years):
                target_year = current_calendar_year + i

                # מדרגות צעד (Step Function): עדכון המחיר רק כאשר מגיעים לשנת המדרגה או עוברים אותה
                for step in sorted_steps:
                    if target_year >= int(step['start_year']):
                        current_price = float(step['monthly_premium'])

                # החלת הנחה במידה ובתוקף
                final_price = current_price
                if discount_percent > 0 and (discount_end_year is None or target_year < discount_end_year):
                    final_price = current_price * (1.0 - (discount_percent / 100.0))

                rates_array.append(round(final_price, 2))

            if len(rates_array) == 0:
                rates_array = [current_premium] * total_policy_years

            return rates_array, detected_age, detected_coverage, "OK", None

        except Exception as e:
            if attempt == max_retries - 1:
                return [], 40, 0, None, str(e)
    return [], 40, 0, None, "Max retries exceeded."


def extract_pdf_premiums(uploaded_file):
    temp_path = None
    max_retries = 3

    for attempt in range(max_retries):
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp.write(uploaded_file.getvalue())
                temp_path = tmp.name

            gemini_file = client.files.upload(file=temp_path)

            prompt = """
            Analyze this Israeli insurance policy PDF. 
            Find the main escalation table ("פרוט השתנות הפרמיה וסכום הביטוח") which lists future monthly premiums line by line.
            Extract:
            1. "client_age" (integer: The accurate current age of the primary insured).
            2. "coverage_amount" (float: Total coverage amount / סכום ביטוח).
            3. "current_premium" (float: The current monthly premium).
            4. "future_premiums" (array of floats: Extract EVERY SINGLE monthly premium rate listed sequentially).

            Return ONLY a valid JSON object with these exact keys: client_age, coverage_amount, current_premium, future_premiums.
            """

            response = client.models.generate_content(
                model='gemini-3.6-flash',
                contents=[gemini_file, prompt]
            )

            response_text = response.text.strip()
            clean_json_str = response_text.replace('```json', '').replace('```', '').strip()
            data = json.loads(clean_json_str)

            detected_age = int(data.get("client_age", 36))
            detected_coverage = float(data.get("coverage_amount", 1000000))
            current_premium = float(data.get("current_premium", 136.09))
            future_premiums = data.get("future_premiums", [])

            rates_array = [current_premium]
            if isinstance(future_premiums, list):
                for p in future_premiums:
                    try:
                        val = float(p)
                        if 0 < val < 50000:
                            rates_array.append(val)
                    except (ValueError, TypeError):
                        continue

            return rates_array, detected_age, detected_coverage, "OK", None
        except Exception as e:
            if attempt == max_retries - 1:
                return [], 36, 1000000, None, str(e)
    return [], 36, 1000000, None, "Max retries exceeded."


def generate_ci_html_report(calc_data, plans, report_title="דוח השוואת ביטוח מחלות קשות", max_expiry_age=75):
    entry_age = calc_data['age']

    max_policy_years = max(1, max_expiry_age - entry_age)
    y70 = max(0, 70 - entry_age)

    base_milestones = [10, 15]
    age_milestones = [yr for yr in [y70] if 0 < yr <= max_policy_years]

    milestones = sorted(list(set(base_milestones + age_milestones + [max_policy_years])))

    plt.figure(figsize=(11, 6))
    markers = ['o', 's', '^', 'D', 'v', 'P', 'X', '*']
    linestyles = ['-', '--', '-.', ':', '-', '-', '--', '-.']

    for i, plan in enumerate(plans):
        key = plan['key']
        name = plan['name']

        y1_cost = calc_data['m_rates'][key].get(1, 0) * 12

        cum_costs = []
        for m_yr in milestones:
            cost = calc_data['cum_res'].get(m_yr, {}).get(key, y1_cost * m_yr)
            cum_costs.append(cost)

        marker_style = markers[i % len(markers)]
        line_style = linestyles[i % len(linestyles)]

        is_existing = (key == "Existing Policy")
        linewidth = 3.5 if is_existing else 2
        alpha_val = 1.0 if is_existing else 0.85
        markersize = 8 if is_existing else 6

        plt.plot(
            milestones, cum_costs,
            label=name,
            marker=marker_style,
            markersize=markersize,
            linestyle=line_style,
            linewidth=linewidth,
            alpha=alpha_val
        )

    plt.title("השוואת עלויות מצטברות לאורך זמן - מחלות קשות (₪)", fontsize=14, fontweight='bold', pad=15)
    plt.xlabel("שנים", fontsize=12)
    plt.ylabel("עלות מצטברת (₪)", fontsize=12)

    plt.xticks(milestones)
    plt.grid(True, linestyle='--', alpha=0.7)
    plt.legend(bbox_to_anchor=(1.02, 1), loc='upper left', fontsize=10)
    plt.tight_layout()

    img_buffer = io.BytesIO()
    plt.savefig(img_buffer, format='png', dpi=150)
    plt.close('all')
    img_base64 = base64.b64encode(img_buffer.getvalue()).decode('utf-8')

    # טבלת אבני דרך ראשית (ללא 3 ו-5 שנים)
    th_milestones_html = ""
    for m_yr in milestones:
        if m_yr == y70:
            th_milestones_html += f"<th>עד גיל 70<br><span style='font-size:11px; font-weight:normal;'>({m_yr} שנים)</span></th>"
        elif m_yr == max_policy_years:
            th_milestones_html += f"<th>מצטבר מלא (עד גיל {max_expiry_age})<br><span style='font-size:11px; font-weight:normal;'>({m_yr} שנים)</span></th>"
        else:
            th_milestones_html += f"<th>סה״כ {m_yr} שנים</th>"

    table_rows = ""
    for plan in plans:
        key = plan['key']
        y1_cost = calc_data['m_rates'][key].get(1, 0) * 12

        td_cells = f"<td style='font-weight: bold;'>{plan['name']}</td>"
        for m_yr in milestones:
            total_cost = calc_data['cum_res'].get(m_yr, {}).get(key, y1_cost * m_yr)
            td_cells += f"<td>₪{total_cost:,.0f}</td>"

        table_rows += f"<tr>{td_cells}</tr>"

    # --- טבלה מורחבת: השוואת פרמיה חודשית לשנים 1 עד 20 ---
    years_to_compare = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 16, 17, 18, 19, 20]
    # לחלופין, אם תרצה טבלה רציפה ממש מ-1 עד 20: list(range(1, 21))

    th_years_html = "".join([f"<th>שנה {yr}</th>" for yr in years_to_compare])

    yearly_rows = ""
    for plan in plans:
        key = plan['key']
        td_cells = f"<td style='font-weight: bold;'>{plan['name']}</td>"
        for yr in years_to_compare:
            monthly_rate = calc_data['m_rates'][key].get(yr, calc_data['m_rates'][key].get(1, 0))
            td_cells += f"<td>₪{monthly_rate:,.2f}</td>"
        yearly_rows += f"<tr>{td_cells}</tr>"

    html_content = f"""
    <!DOCTYPE html>
    <html dir="rtl" lang="he">
    <head>
        <meta charset="utf-8">
        <title>{report_title}</title>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.1/html2pdf.bundle.min.js"></script>
        <style>
            body {{ font-family: Arial, sans-serif; color: #1E293B; margin: 30px auto; max-width: 1100px; direction: rtl; text-align: right; }}
            h1 {{ color: #0D9488; border-bottom: 2px solid #CCFBF1; padding-bottom: 10px; }}
            .details {{ background: #F0FDFA; padding: 15px; border-radius: 8px; border: 1px solid #99F6E4; margin-bottom: 20px; display: flex; justify-content: space-between; }}
            .chart-container {{ text-align: center; margin: 25px 0; }}
            .table-responsive {{ overflow-x: auto; margin-top: 20px; }}
            table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
            th, td {{ padding: 8px; text-align: center; border: 1px solid #E2E8F0; white-space: nowrap; }}
            th {{ background-color: #F8FAFC; color: #334155; font-weight: bold; }}
            tr:nth-child(even) {{ background-color: #F8FAFC; }}
            h3 {{ margin-top: 35px; color: #115E59; }}
        </style>
    </head>
    <body>
        <div data-html2canvas-ignore="true" style="text-align: center; margin-bottom: 20px; padding: 15px; background-color: #F0FDFA; border: 1px solid #99F6E4; border-radius: 8px;">
            <p style="margin: 0; font-size: 16px; font-weight: bold; color: #115E59;">הדוח מוכן!</p>
            <button onclick="downloadPDF()" style="margin-top: 15px; padding: 10px 20px; background-color: #0D9488; color: white; border: none; border-radius: 5px; cursor: pointer; font-size: 18px; font-weight: bold;">
                📥 הורד קובץ PDF
            </button>
        </div>

        <div id="pdf-content" style="padding: 20px; background: white;">
            <h1>Life Smart - {report_title}</h1>
            <div class="details">
                <span><strong>גיל מבוטח:</strong> {entry_age}</span>
                <span><strong>סכום ביטוח:</strong> ₪{calc_data['coverage']:,}</span>
                <span><strong>סטטוס עישון:</strong> {calc_data['smoker']}</span>
            </div>
            <h2>תחזית מלאה עד גיל {max_expiry_age} (משך פוליסה: {max_policy_years} שנים)</h2>
            <div class="chart-container">
                <img src="data:image/png;base64,{img_base64}" width="100%" />
            </div>

            <h2>ריכוז עלויות ואבני דרך מצטברות</h2>
            <table>
                <thead>
                    <tr>
                        <th>תכנית ביטוח</th>
                        {th_milestones_html}
                    </tr>
                </thead>
                <tbody>
                    {table_rows}
                </tbody>
            </table>

            <h3>השוואת פרמיה חודשית (שנים 1–10 ושנים 15–20)</h3>
            <div class="table-responsive">
                <table>
                    <thead>
                        <tr>
                            <th>תכנית ביטוח</th>
                            {th_years_html}
                        </tr>
                    </thead>
                    <tbody>
                        {yearly_rows}
                    </tbody>
                </table>
            </div>
        </div>

        <script>
            function downloadPDF() {{
                const element = document.getElementById('pdf-content');
                const opt = {{
                    margin:       10,
                    filename:     'LifeSmart_CI_Report_{entry_age}.pdf',
                    image:        {{ type: 'jpeg', quality: 0.98 }},
                    html2canvas:  {{ scale: 2 }},
                    jsPDF:        {{ unit: 'mm', format: 'a4', orientation: 'landscape' }}
                }};
                html2pdf().set(opt).from(element).save();
            }}
        </script>
    </body>
    </html>
    """
    return html_content.encode('utf-8')

def generate_html_report(calc_data, plans, report_title="דוח השוואת חלופות ביטוח חיים", max_expiry_age=80):
    entry_age = calc_data['age']

    max_policy_years = max(1, max_expiry_age - entry_age)
    y70 = max(0, 70 - entry_age)
    y75 = max(0, 75 - entry_age)
    y80 = max(0, 80 - entry_age)

    base_milestones = [3, 5, 10, 15]
    if max_expiry_age == 75:
        age_milestones = [yr for yr in [y70, y75] if 0 < yr <= max_policy_years]
    else:
        age_milestones = [yr for yr in [y70, y75, y80] if 0 < yr <= max_policy_years]

    milestones = sorted(list(set(base_milestones + age_milestones + [max_policy_years])))

    plt.figure(figsize=(11, 6))
    markers = ['o', 's', '^', 'D', 'v', 'P', 'X', '*']
    linestyles = ['-', '--', '-.', ':', '-', '-', '--', '-.']

    for i, plan in enumerate(plans):
        key = plan['key']
        name = plan['name']

        y1_cost = calc_data['m_rates'][key].get(1, 0) * 12

        cum_costs = []
        for m_yr in milestones:
            cost = calc_data['cum_res'].get(m_yr, {}).get(key, y1_cost * m_yr)
            cum_costs.append(cost)

        marker_style = markers[i % len(markers)]
        line_style = linestyles[i % len(linestyles)]

        is_existing = (key == "Existing Policy")
        linewidth = 3.5 if is_existing else 2
        alpha_val = 1.0 if is_existing else 0.85
        markersize = 8 if is_existing else 6

        plt.plot(
            milestones, cum_costs,
            label=name,
            marker=marker_style,
            markersize=markersize,
            linestyle=line_style,
            linewidth=linewidth,
            alpha=alpha_val
        )

    plt.title("השוואת עלויות מצטברות לאורך זמן (₪)", fontsize=14, fontweight='bold', pad=15)
    plt.xlabel("שנים", fontsize=12)
    plt.ylabel("עלות מצטברת (₪)", fontsize=12)

    plt.xticks(milestones)
    plt.grid(True, linestyle='--', alpha=0.7)
    plt.legend(bbox_to_anchor=(1.02, 1), loc='upper left', fontsize=10)
    plt.tight_layout()

    img_buffer = io.BytesIO()
    plt.savefig(img_buffer, format='png', dpi=150)
    plt.close('all')
    img_base64 = base64.b64encode(img_buffer.getvalue()).decode('utf-8')

    th_milestones_html = ""
    for m_yr in milestones:
        if m_yr == y70:
            th_milestones_html += f"<th>עד גיל 70<br><span style='font-size:11px; font-weight:normal;'>({m_yr} שנים)</span></th>"
        elif m_yr == y75:
            th_milestones_html += f"<th>עד גיל 75<br><span style='font-size:11px; font-weight:normal;'>({m_yr} שנים)</span></th>"
        elif m_yr == y80 and max_expiry_age == 80:
            th_milestones_html += f"<th>עד גיל 80<br><span style='font-size:11px; font-weight:normal;'>({m_yr} שנים)</span></th>"
        elif m_yr == max_policy_years:
            th_milestones_html += f"<th>מצטבר מלא (עד גיל {max_expiry_age})<br><span style='font-size:11px; font-weight:normal;'>({m_yr} שנים)</span></th>"
        else:
            th_milestones_html += f"<th>סה״כ {m_yr} שנים</th>"

    table_rows = ""
    for plan in plans:
        key = plan['key']
        y1_cost = calc_data['m_rates'][key].get(1, 0) * 12

        td_cells = f"<td style='font-weight: bold;'>{plan['name']}</td>"
        for m_yr in milestones:
            total_cost = calc_data['cum_res'].get(m_yr, {}).get(key, y1_cost * m_yr)
            td_cells += f"<td>₪{total_cost:,.0f}</td>"

        table_rows += f"<tr>{td_cells}</tr>"

    html_content = f"""
    <!DOCTYPE html>
    <html dir="rtl" lang="he">
    <head>
        <meta charset="utf-8">
        <title>{report_title}</title>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.1/html2pdf.bundle.min.js"></script>
        <style>
            body {{ font-family: Arial, sans-serif; color: #1E293B; margin: 30px auto; max-width: 950px; direction: rtl; text-align: right; }}
            h1 {{ color: #1D4ED8; border-bottom: 2px solid #DCE7F5; padding-bottom: 10px; }}
            .details {{ background: #FAFCFF; padding: 15px; border-radius: 8px; border: 1px solid #DCE7F5; margin-bottom: 20px; display: flex; justify-content: space-between; }}
            .chart-container {{ text-align: center; margin: 25px 0; page-break-inside: avoid; page-break-after: avoid; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 20px; font-size: 14px; page-break-inside: avoid; }}
            th, td {{ padding: 10px; text-align: center; border: 1px solid #E2E8F0; }}
            th {{ background-color: #F1F5F9; color: #334155; font-weight: bold; }}
            tr {{ page-break-inside: avoid; page-break-after: auto; }}
            tr:nth-child(even) {{ background-color: #F8FAFC; }}
            .table-container {{ page-break-inside: avoid; }}
        </style>
    </head>
    <body>
        <div data-html2canvas-ignore="true" style="text-align: center; margin-bottom: 20px; padding: 15px; background-color: #EFF6FF; border: 1px solid #BFDBFE; border-radius: 8px;">
            <p style="margin: 0; font-size: 16px; font-weight: bold; color: #1E3A8A;">הדוח מוכן!</p>
            <button onclick="downloadPDF()" style="margin-top: 15px; padding: 10px 20px; background-color: #1D4ED8; color: white; border: none; border-radius: 5px; cursor: pointer; font-size: 18px; font-weight: bold;">
                📥 הורד קובץ PDF
            </button>
        </div>

        <div id="pdf-content" style="padding: 20px; background: white;">
            <h1>Life Smart - {report_title}</h1>
            <div class="details">
                <span><strong>גיל מבוטח:</strong> {entry_age}</span>
                <span><strong>סכום ביטוח:</strong> ₪{calc_data['coverage']:,}</span>
                <span><strong>סטטוס עישון:</strong> {calc_data['smoker']}</span>
            </div>
            <h2>תחזית מלאה עד גיל {max_expiry_age} (משך פוליסה: {max_policy_years} שנים)</h2>
            <div class="chart-container">
                <img src="data:image/png;base64,{img_base64}" width="100%" />
            </div>
            <div class="table-container">
                <h2>ריכוז עלויות ואבני דרך</h2>
                <table>
                    <thead>
                        <tr>
                            <th>תכנית ביטוח</th>
                            {th_milestones_html}
                        </tr>
                    </thead>
                    <tbody>
                        {table_rows}
                    </tbody>
                </table>
            </div>
        </div>

        <script>
            function downloadPDF() {{
                const element = document.getElementById('pdf-content');
                const opt = {{
                    margin:       10,
                    filename:     'LifeSmart_Report_{entry_age}.pdf',
                    image:        {{ type: 'jpeg', quality: 0.98 }},
                    html2canvas:  {{ scale: 2 }},
                    jsPDF:        {{ unit: 'mm', format: 'a4', orientation: 'portrait' }}
                }};
                html2pdf().set(opt).from(element).save();
            }}
        </script>
    </body>
    </html>
    """
    return html_content.encode('utf-8')

st.set_page_config(layout="wide", page_title="אוליבר ביטוחים - השוואת ביטוח חיים")

# --- Custom CSS ---
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Assistant:wght@400;600;700;800&display=swap');
    html, body, [class*="css"] { font-family: 'Assistant', sans-serif; direction: rtl; text-align: right; }
    #MainMenu, footer, header {visibility: hidden;}
    .block-container { padding-top: 1.5rem; padding-bottom: 2rem; max-width: 1050px; }
    .st-card { background: #FFFFFF; border: 1px solid #E6EEF8; border-radius: 20px; padding: 24px; box-shadow: 0 4px 14px rgba(22, 53, 107, 0.05); margin-bottom: 20px; }
    .app-title { font-size: 34px; font-weight: 800; color: #102A43; text-align: center; margin-bottom: 20px; }
    .deal-card { background: white; border: 1px solid #E6EFF9; border-radius: 16px; padding: 16px 24px; margin-bottom: 14px; box-shadow: 0 2px 8px rgba(0,0,0,0.03); display: flex; align-items: center; justify-content: space-between; }
    .deal-price { font-size: 32px; font-weight: 800; color: #0F172A; }
    .deal-subprice { font-size: 15px; font-weight: 700; color: #059669; }
    .pill-badge { background-color: #E8F5E9; color: #2E7D32; padding: 4px 12px; border-radius: 20px; font-size: 13px; font-weight: 700; display: inline-block; }
</style>
""", unsafe_allow_html=True)




# --- 1. Engine Setup (Life & CI) ---
class PremiumEngine:
    def __init__(self):
        self.data_loaded = True
        base_dir = os.path.dirname(os.path.abspath(__file__))

        path_under = os.path.join(base_dir, 'risk1_base_rates.csv')
        path_over = os.path.join(base_dir, 'risk1_base_rates_over_500k.csv')
        path_clal = os.path.join(base_dir, 'clal_rates.xlsx')
        path_phenix = os.path.join(base_dir, 'phenix_rates.csv')
        path_ayalon_ci = os.path.join(base_dir, 'ci_ayalon_rates.csv')

        try:
            self.rates_under_500k = pd.read_csv(path_under).set_index('Age')
            self.rates_over_500k = pd.read_csv(path_over).set_index('Age')
            self.phenix_rates = pd.read_csv(path_phenix).set_index('Age')
        except Exception:
            self.data_loaded = False

        try:
            df_clal = pd.read_excel(path_clal, sheet_name=0, header=None)
            clal_data = df_clal.iloc[5:, 1:10].copy()
            clal_data.columns = [
                'Age', 'Male_NonSmoker_Under', 'Male_Smoker_Under', 'Female_NonSmoker_Under', 'Female_Smoker_Under',
                'Male_NonSmoker_Over', 'Male_Smoker_Over', 'Female_NonSmoker_Over', 'Female_Smoker_Over'
            ]
            clal_data['Age'] = pd.to_numeric(clal_data['Age'], errors='coerce')
            self.clal_rates = clal_data.dropna(subset=['Age']).set_index('Age')
        except Exception:
            self.data_loaded = False

        try:
            self.ayalon_ci_rates = pd.read_csv(path_ayalon_ci).set_index('Age')
        except Exception:
            self.ayalon_ci_rates = None

    def get_base_premium(self, age, profile, coverage_amount, company="Menora"):
        if not self.data_loaded: return 0
        if company == "Menora":
            if age > 79: age = 79
            try:
                table = self.rates_over_500k if coverage_amount > 500000 else self.rates_under_500k
                return table.loc[age, profile] * (coverage_amount / 100000)
            except KeyError:
                return 0
        elif company == "CLAL":
            if age > 99: age = 99
            try:
                col = f"{profile}_Over" if coverage_amount > 500000 else f"{profile}_Under"
                rate_annual = self.clal_rates.loc[age, col]
                return (rate_annual * (coverage_amount / 1000)) / 12
            except KeyError:
                return 0
        elif company == "Phenix":
            if age > 79: age = 79
            try:
                rate_annual = self.phenix_rates.loc[age, profile]
                return (rate_annual * (coverage_amount / 1000)) / 12
            except KeyError:
                return 0

    def get_ci_base_premium(self, age, profile, coverage_amount, company="Ayalon"):
        if self.ayalon_ci_rates is None: return 0
        if company == "Ayalon":
            lookup_age = min(age, 65)
            try:
                rate_monthly = self.ayalon_ci_rates.loc[lookup_age, profile]
                return rate_monthly * (coverage_amount / 1000)
            except KeyError:
                return 0


engine = PremiumEngine()


# --- 2A. Calculation Logic (Life Insurance) ---
def compute_all_rates(entry_age, gender, is_smoker, coverage, existing_rates=None):
    profile = f"{gender}_{'Smoker' if is_smoker else 'NonSmoker'}"
    base_menora = engine.get_base_premium(entry_age, profile, coverage, "Menora")
    base_clal = engine.get_base_premium(entry_age, profile, coverage, "CLAL")
    base_phenix = engine.get_base_premium(entry_age, profile, coverage, "Phenix")

    plans_to_track = [
        "base price", "Risk 10 CLAL", "Risk 15 CLAL", "Risk 15 Menora", "Menora risk 1",
        "Phenix Risk 15 (30%)", "Phenix Risk 15 (40%)"
    ]
    if existing_rates: plans_to_track.append("Existing Policy")

    max_expiry_age = 80
    total_policy_years = max(1, max_expiry_age - entry_age)
    total_months = total_policy_years * 12

    costs = {p: 0 for p in plans_to_track}
    monthly_rates = {p: {} for p in plans_to_track}

    y70 = max(0, 70 - entry_age)
    y75 = max(0, 75 - entry_age)
    y80 = max(0, 80 - entry_age)

    target_years = [3, 5, 10, 15]
    for y in [y70, y75, y80]:
        if 0 < y <= total_policy_years and y not in target_years:
            target_years.append(y)

    if total_policy_years not in target_years:
        target_years.append(total_policy_years)

    target_years = sorted(target_years)
    target_months = {yr * 12 for yr in target_years if yr > 0}
    cum_results = {yr: {} for yr in target_years if yr > 0}

    for m in range(1, total_months + 1):
        yr = ((m - 1) // 12) + 1
        age = entry_age + yr - 1

        curr_m = engine.get_base_premium(age, profile, coverage, "Menora")
        curr_c = engine.get_base_premium(age, profile, coverage, "CLAL")
        curr_p = engine.get_base_premium(age, profile, coverage, "Phenix")

        m_r1 = curr_m
        m_r10c = (base_clal * 0.85) if yr <= 10 else curr_c
        m_r15c = (base_clal * 0.85) if yr <= 15 else curr_c

        if yr == 1:
            m_r15m = base_menora * 0.85
        elif yr == 2:
            m_r15m = base_menora * 0.90
        elif yr == 3:
            m_r15m = base_menora * 0.95
        elif yr <= 15:
            m_r15m = base_menora
        else:
            m_r15m = curr_m * 0.70

        if yr == 1:
            m_custom = curr_m * (1 - 0.60)
        elif yr == 2:
            m_custom = curr_m * (1 - 0.55)
        elif yr == 3:
            m_custom = curr_m * (1 - 0.50)
        elif yr == 4:
            m_custom = curr_m * (1 - 0.40)
        elif yr == 5:
            m_custom = curr_m * (1 - 0.30)
        elif yr == 6:
            m_custom = curr_m * (1 - 0.25)
        elif 7 <= yr <= 10:
            m_custom = curr_m * (1 - 0.20)
        else:
            m_custom = curr_m

        if yr <= 6:
            m_phenix_30 = base_phenix * (1 - 0.30)
        elif yr <= 15:
            m_phenix_30 = base_phenix
        else:
            m_phenix_30 = curr_p

        if yr <= 4:
            m_phenix_40 = base_phenix * (1 - 0.40)
        elif yr <= 15:
            m_phenix_40 = base_phenix
        else:
            m_phenix_40 = curr_p

        costs["base price"] += m_r1
        costs["Risk 10 CLAL"] += m_r10c
        costs["Risk 15 CLAL"] += m_r15c
        costs["Risk 15 Menora"] += m_r15m
        costs["Menora risk 1"] += m_custom
        costs["Phenix Risk 15 (30%)"] += m_phenix_30
        costs["Phenix Risk 15 (40%)"] += m_phenix_40

        m_rates_map = {
            "base price": m_r1,
            "Risk 10 CLAL": m_r10c,
            "Risk 15 CLAL": m_r15c,
            "Risk 15 Menora": m_r15m,
            "Menora risk 1": m_custom,
            "Phenix Risk 15 (30%)": m_phenix_30,
            "Phenix Risk 15 (40%)": m_phenix_40
        }

        if existing_rates:
            idx = yr - 1
            if idx < len(existing_rates):
                m_ext = existing_rates[idx]
            else:
                m_ext = existing_rates[-1]
            costs["Existing Policy"] += m_ext
            m_rates_map["Existing Policy"] = m_ext

        if m == 1:
            for p in plans_to_track:
                monthly_rates[p][1] = m_rates_map[p]

        if m in target_months:
            matching_yrs = [t_yr for t_yr in target_years if t_yr * 12 == m]
            for t_yr in matching_yrs:
                for p in plans_to_track:
                    cum_results[t_yr][p] = costs[p]
                    monthly_rates[p][t_yr] = m_rates_map[p]

    return cum_results, monthly_rates


# --- 2B. Calculation Logic (Critical Illness) ---
def compute_ci_rates(entry_age, gender, is_smoker, coverage, existing_rates=None):
    profile = f"{gender}_{'Smoker' if is_smoker else 'NonSmoker'}"

    plans_to_track = ["Ayalon CI - Base", "Ayalon CI - 40%"]
    if coverage < 250000:
        plans_to_track.append("Ayalon CI - 30%")
    else:
        plans_to_track.append("Ayalon CI - 35%")

    if existing_rates: plans_to_track.append("Existing Policy")

    max_expiry_age = 75
    total_policy_years = max(1, max_expiry_age - entry_age)
    total_months = total_policy_years * 12

    costs = {p: 0 for p in plans_to_track}
    monthly_rates = {p: {} for p in plans_to_track}

    y70 = max(0, 70 - entry_age)
    y75 = max(0, 75 - entry_age)

    target_years = [10, 15]
    for y in [y70]:
        if 0 < y <= total_policy_years and y not in target_years:
            target_years.append(y)

    if total_policy_years not in target_years:
        target_years.append(total_policy_years)

    target_years = sorted(target_years)
    target_months = {yr * 12 for yr in target_years if yr > 0}
    cum_results = {yr: {} for yr in target_years if yr > 0}

    for m in range(1, total_months + 1):
        yr = ((m - 1) // 12) + 1
        age = entry_age + yr - 1

        curr_a = engine.get_ci_base_premium(age, profile, coverage, "Ayalon")
        if curr_a == 0:
            curr_a = engine.get_ci_base_premium(min(age, 65), profile, coverage, "Ayalon")

        # הנחה ל-10 שנים ראשונות בלבד באיילון
        if yr <= 10:
            m_base = curr_a
            m_40 = curr_a * 0.60
            m_30 = curr_a * 0.70
            m_35 = curr_a * 0.65
        else:
            # אחרי שנה 10 - אין הנחות, כולם במחיר המלא לפי גיל
            m_base = curr_a
            m_40 = curr_a
            m_30 = curr_a
            m_35 = curr_a

        costs["Ayalon CI - Base"] += m_base
        costs["Ayalon CI - 40%"] += m_40

        m_rates_map = {
            "Ayalon CI - Base": m_base,
            "Ayalon CI - 40%": m_40
        }

        if coverage < 250000:
            costs["Ayalon CI - 30%"] += m_30
            m_rates_map["Ayalon CI - 30%"] = m_30
        else:
            costs["Ayalon CI - 35%"] += m_35
            m_rates_map["Ayalon CI - 35%"] = m_35

        if existing_rates:
            idx = yr - 1
            if idx < len(existing_rates):
                m_ext = existing_rates[idx]
            else:
                m_ext = existing_rates[-1]
            costs["Existing Policy"] += m_ext
            m_rates_map["Existing Policy"] = m_ext

        # קריטי: שמירת המחיר החודשי עבור כל שנה ושנה במפורש (כולל 15, 16, 17, 18, 19, 20)
        for p in plans_to_track:
            monthly_rates[p][yr] = m_rates_map[p]

        if m in target_months:
            matching_yrs = [t_yr for t_yr in target_years if t_yr * 12 == m]
            for t_yr in matching_yrs:
                for p in plans_to_track:
                    cum_results[t_yr][p] = costs[p]

    return cum_results, monthly_rates


# --- State Initialization ---
if 'view_mode' not in st.session_state: st.session_state.view_mode = 'input'
if 'calc_data' not in st.session_state: st.session_state.calc_data = None
if 'mem_age_mode' not in st.session_state: st.session_state.mem_age_mode = "גיל"
if 'mem_dob' not in st.session_state: st.session_state.mem_dob = date(1990, 1, 1)
if 'mem_age' not in st.session_state: st.session_state.mem_age = 36
if 'mem_gender' not in st.session_state: st.session_state.mem_gender = "זכר"
if 'mem_smoker' not in st.session_state: st.session_state.mem_smoker = False
if 'mem_coverage' not in st.session_state: st.session_state.mem_coverage = 1000000
if 'mem_existing_rates' not in st.session_state: st.session_state.mem_existing_rates = None


@st.dialog("הזנת פוליסה קיימת - עידכון ידני", width="large")
def manual_entry_dialog():
    st.markdown(
        "הזן את נתוני המבוטח ואת העלות החודשית הצפויה לכל שנת פוליסה. שמירת הנתונים כאן תעדכן אוטומטית את בסיס ההשוואה.")
    c1, c2, c3, c4 = st.columns(4)
    m_age = c1.number_input("גיל התחלתי", min_value=18, max_value=75, value=int(st.session_state.mem_age))
    m_coverage = c2.number_input("סכום ביטוח", min_value=50000, max_value=10000000,
                                 value=int(st.session_state.mem_coverage), step=50000)
    m_gender = c3.selectbox("מין", ["זכר", "נקבה"], index=0 if st.session_state.mem_gender == "זכר" else 1)
    m_smoker = c4.checkbox("מעשן", value=st.session_state.mem_smoker)

    st.markdown("---")
    st.markdown("### פרמיה חודשית (₪) לפי שנת פוליסה")
    rates = []
    for row in range(3):
        cols = st.columns(5)
        for col_idx in range(5):
            yr = (row * 5) + col_idx + 1
            val = 0.0
            if st.session_state.mem_existing_rates and len(st.session_state.mem_existing_rates) >= yr:
                val = float(st.session_state.mem_existing_rates[yr - 1])
            rate = cols[col_idx].number_input(f"שנה {yr}", min_value=0.0, value=val, step=10.0, format="%.2f",
                                              key=f"rate_{yr}")
            rates.append(rate)

    if st.button("שמור פוליסה ועדכן מחשבון", type="primary", use_container_width=True):
        st.session_state.mem_age = m_age
        st.session_state.mem_coverage = m_coverage
        st.session_state.mem_gender = "זכר" if m_gender == "זכר" else "נקבה"
        st.session_state.mem_smoker = m_smoker
        st.session_state.mem_existing_rates = rates
        st.session_state.last_processed_file = "MANUAL_ENTRY"
        st.rerun()


# ==========================================
# PHASE 1: INPUT SCREEN
# ==========================================
if st.session_state.view_mode == 'input':
    c1, c2 = st.columns([1, 1])
    with c1:
        st.markdown("<span style='font-size:20px; font-weight:800; color:#1D4ED8;'>🧮 LIFE <b>SMART.</b></span>",
                    unsafe_allow_html=True)
    with c2:
        st.markdown("<div style='text-align:left;'><span class='pill-badge'>● ביטוח חיים + מחלות קשות</span></div>",
                    unsafe_allow_html=True)

    st.markdown("<div class='app-title'>השוואת תעריפי ביטוח</div>", unsafe_allow_html=True)

    st.markdown("""
    <div style='background: white; border: 1px solid #DCE7F5; border-radius: 18px; padding: 20px;'>
        <div style='font-weight: 800; font-size: 18px; color: #1E293B; margin-bottom: 12px;'>פרטי מבוטח</div>
    """, unsafe_allow_html=True)

    age_input_mode = st.radio("סוג קלט גיל", ["גיל", "תאריך לידה"], horizontal=True, label_visibility="collapsed",
                              index=0 if st.session_state.mem_age_mode == 'גיל' else 1)
    if age_input_mode == "תאריך לידה":
        dob = st.date_input("תאריך לידה", value=st.session_state.mem_dob, min_value=date(1940, 1, 1),
                            max_value=date.today())
        client_age = date.today().year - dob.year
    else:
        client_age = st.number_input("גיל", min_value=0, max_value=75, value=int(st.session_state.mem_age))

    gender_choice = st.radio("מין", ["זכר", "נקבה"], horizontal=True,
                             index=0 if st.session_state.mem_gender == 'זכר' else 1)
    is_smoker = st.checkbox("מעשן / לא מעשן (סמן אם מעשן)", value=st.session_state.mem_smoker)
    st.markdown("</div>", unsafe_allow_html=True)

    st.write("")
    st.markdown("""
    <div style='background: white; border: 1px solid #DCE7F5; border-radius: 18px; padding: 20px; text-align: center;'>
        <div style='font-size: 15px; color: #64748B; font-weight: 700;'>סכום ביטוח מבוקש (₪)</div>
    """, unsafe_allow_html=True)
    coverage_val = st.number_input("סכום ביטוח", min_value=50000, max_value=10000000,
                                   value=int(st.session_state.mem_coverage), step=50000, label_visibility="collapsed")
    st.markdown("</div>", unsafe_allow_html=True)

    st.write("")
    st.markdown("""
        <div style='background: white; border: 1px solid #DCE7F5; border-radius: 18px; padding: 20px; text-align: center;'>
            <div style='font-size: 15px; color: #64748B; font-weight: 700; margin-bottom: 10px;'>הוספת נתוני פוליסה קיימת להשוואה</div>
        """, unsafe_allow_html=True)

    col_upload, col_manual = st.columns([3, 1])
    with col_upload:
        uploaded_pdf = st.file_uploader("גרור או בחר קובץ PDF של הפוליסה הנוכחית", type=["pdf"],
                                        label_visibility="collapsed")
    with col_manual:
        st.write("")
        if st.button("✍ הזנה ידנית", use_container_width=True):
            manual_entry_dialog()

    # שמירת הקובץ בזיכרון ה-Session כדי לא לאבד אותו במעברים
    if uploaded_pdf is not None:
        st.session_state.uploaded_pdf_bytes = uploaded_pdf.getvalue()
        st.session_state.uploaded_pdf_name = uploaded_pdf.name
        st.session_state.uploaded_pdf_size = uploaded_pdf.size

    # שימוש בקובץ מהזיכרון אם קיים
    active_pdf_file = uploaded_pdf
    if active_pdf_file is None and 'uploaded_pdf_bytes' in st.session_state:
        active_pdf_file = io.BytesIO(st.session_state.uploaded_pdf_bytes)
        active_pdf_file.name = st.session_state.uploaded_pdf_name
        active_pdf_file.size = st.session_state.uploaded_pdf_size

    if active_pdf_file is not None:
        file_signature = f"{getattr(active_pdf_file, 'name', 'file')}_{getattr(active_pdf_file, 'size', 0)}"

        if st.session_state.get('last_processed_file') != file_signature:
            with st.spinner("🤖 סורק את הפוליסה לזיהוי המבוטחים והגילאים האמיתיים..."):
                members, error_msg = extract_policy_members(active_pdf_file)

            if error_msg:
                st.error(f"⚠️ שגיאה בסריקת הקובץ: {error_msg}")
                st.session_state.detected_members = []
            elif members and len(members) > 0:
                st.session_state.detected_members = members
                st.session_state.last_processed_file = file_signature
            else:
                st.session_state.detected_members = []

        if st.session_state.get('detected_members') and len(st.session_state.detected_members) > 0:
            members = st.session_state.detected_members

            member_options = {}
            for m in members:
                name = m.get('name', 'לא ידוע')
                curr_age = m.get('current_age')
                birth = m.get('birth_date')

                parts = [name]
                if curr_age:
                    parts.append(f"גיל נוכחי: {curr_age}")
                elif birth:
                    parts.append(f"ת.לידה: {birth}")

                display_label = " | ".join(parts)
                member_options[display_label] = m

            st.info(f"📋 זוהו {len(members)} מבוטחים במסמך זה. בחר מבוטח להשוואה:")
            selected_label = st.selectbox("בחר מבוטח:", list(member_options.keys()), key="phase1_member_select",
                                          label_visibility="collapsed")
            st.session_state.selected_member_choice = member_options[selected_label]
        else:
            st.info("💾 העלה קובץ PDF עם מספר מבוטחים או הזן ידנית.")

    else:
        current_existing_rates = st.session_state.mem_existing_rates
        if current_existing_rates:
            if st.session_state.get('last_processed_file') == "MANUAL_ENTRY":
                st.success(
                    f"✅ פוליסה ידנית נשמרה (גיל מבוטח: {st.session_state.mem_age} | סכום ביטוח: ₪{st.session_state.mem_coverage:,.0f})")
            else:
                st.info("💾 נתוני פוליסה קיימת שמורים במערכת.")

    st.markdown("</div>", unsafe_allow_html=True)
    st.write("")

    col_life, col_ci = st.columns(2)
    with col_life:
        if st.button("🛡️ השוואת ביטוח חיים", type="primary", use_container_width=True):
            st.session_state.mem_age_mode = age_input_mode
            if age_input_mode == "תאריך לידה": st.session_state.mem_dob = dob

            chosen_m = st.session_state.get('selected_member_choice')
            if chosen_m and 'uploaded_pdf_bytes' in st.session_state:
                pdf_file_obj = io.BytesIO(st.session_state.uploaded_pdf_bytes)
                selected_name = chosen_m.get('name', 'מבוטח')
                selected_id = chosen_m.get('id_num', '') or chosen_m.get('policy_num', '')

                with st.spinner(f"מחלץ נתוני ביטוח חיים וגיל מדויק עבור {selected_name}..."):
                    extracted, detected_age, detected_coverage, debug_text, error_msg = extract_targeted_policy_premiums(
                        pdf_file_obj, target_name=selected_name, target_id=selected_id, insurance_type="life"
                    )
                if error_msg:
                    st.error(f"⚠️ {error_msg}")
                    st.stop()
                else:
                    client_age = detected_age
                    coverage_val = detected_coverage
                    current_existing_rates = extracted
                    st.session_state.mem_existing_rates = extracted

            cum_res, m_rates = compute_all_rates(client_age, "Male" if gender_choice == "זכר" else "Female", is_smoker,
                                                 coverage_val, current_existing_rates)
            st.session_state.calc_data = {
                "client_name": st.session_state.get('selected_member_choice', {}).get('name', 'לקוח'),
                "age": client_age, "gender": "Male" if gender_choice == "זכר" else "Female",
                "smoker": "מעשן" if is_smoker else "לא מעשן", "coverage": coverage_val, "cum_res": cum_res,
                "m_rates": m_rates
            }
            st.session_state.view_mode = 'results_life'
            st.rerun()

    with col_ci:
        if st.button("🩺 השוואת מחלות קשות", type="secondary", use_container_width=True):
            st.session_state.mem_age_mode = age_input_mode
            if age_input_mode == "תאריך לידה": st.session_state.mem_dob = dob

            chosen_m = st.session_state.get('selected_member_choice')
            if chosen_m and 'uploaded_pdf_bytes' in st.session_state:
                pdf_file_obj = io.BytesIO(st.session_state.uploaded_pdf_bytes)
                selected_name = chosen_m.get('name', 'מבוטח')
                selected_id = chosen_m.get('id_num', '') or chosen_m.get('policy_num', '')

                with st.spinner(f"מחלץ נתוני מחלות קשות וגיל מדויק עבור {selected_name}..."):
                    extracted, detected_age, detected_coverage, debug_text, error_msg = extract_targeted_policy_premiums(
                        pdf_file_obj, target_name=selected_name, target_id=selected_id, insurance_type="ci"
                    )
                if error_msg:
                    st.error(f"⚠️ {error_msg}")
                    st.stop()
                else:
                    client_age = detected_age
                    coverage_val = detected_coverage
                    current_existing_rates = extracted
                    st.session_state.mem_existing_rates = extracted

            cum_res, m_rates = compute_ci_rates(client_age, "Male" if gender_choice == "זכר" else "Female", is_smoker,
                                                coverage_val, current_existing_rates)
            st.session_state.calc_data = {
                "client_name": st.session_state.get('selected_member_choice', {}).get('name', 'לקוח'),
                "age": client_age, "gender": "Male" if gender_choice == "זכר" else "Female",
                "smoker": "מעשן" if is_smoker else "לא מעשן", "coverage": coverage_val, "cum_res": cum_res,
                "m_rates": m_rates
            }
            st.session_state.view_mode = 'results_ci'
            st.rerun()


# ==========================================
# PHASE 2: RESULTS SCREEN - LIFE INSURANCE
# ==========================================
elif st.session_state.view_mode == 'results_life':
    c = st.session_state.calc_data
    entry_age = c['age']

    plans = []
    if "Existing Policy" in c['m_rates']:
        plans.append({"name": "הפוליסה הנוכחית", "company": "נתונים מתוך דוח מצורף", "badge": "מצב קיים",
                      "key": "Existing Policy"})

    plans.extend([
        {"name": "ריסק 15 הפניקס (30%)", "company": "הפניקס", "badge": "קיבוע ל-15 שנים (30% הנחה ל-6 שנ')",
         "key": "Phenix Risk 15 (30%)"},
        {"name": "ריסק 15 הפניקס (40%)", "company": "הפניקס", "badge": "קיבוע ל-15 שנים (40% הנחה ל-4 שנ')",
         "key": "Phenix Risk 15 (40%)"},
        {"name": "ריסק 15 מנורה", "company": "מנורה מבטחים", "badge": "קיבוע ל-15 שנים + 30% הנחה",
         "key": "Risk 15 Menora"},
        {"name": "מנורה - ריסק 1(מדורג)", "company": "מנורה מבטחים", "badge": "הנחות משתנות (60% עד 20%)",
         "key": "Menora risk 1"},
        {"name": "ריסק 15 כלל", "company": "כלל ביטוח", "badge": "קיבוע ל-15 שנים (15% הנחה)", "key": "Risk 15 CLAL"},
        {"name": "ריסק 10 כלל", "company": "כלל ביטוח", "badge": "קיבוע ל-10 שנים (15% הנחה)", "key": "Risk 10 CLAL"},
        {"name": "ללא הנחות", "company": "מנורה מבטחים", "badge": "מתעדכן כל שנה לפי גיל", "key": "base price"}
    ])

    top_col1, top_col2, top_col3 = st.columns([1, 2, 1.2])
    with top_col1:
        if st.button("✏️ עריכת פרטים", use_container_width=True):
            st.session_state.view_mode = 'input'
            st.rerun()

    with top_col2:
        st.markdown(
            f"<div style='text-align: center; font-weight:700;'>🛡️ כיסוי חיים: <b>₪{c['coverage']:,}</b> &nbsp;|&nbsp; 👤 מבוטח: <b>{c.get('client_name', '')} (גיל {entry_age})</b></div>",
            unsafe_allow_html=True)

    with top_col3:
        if st.button("📄 הצג דוח מלא", type="primary", use_container_width=True):
            with st.spinner("מכין דוח..."):
                try:
                    raw_html = generate_html_report(c, plans, report_title="דוח השוואת ביטוח חיים", max_expiry_age=80)
                    fd, temp_path = tempfile.mkstemp(suffix=".html")
                    with os.fdopen(fd, 'wb') as f:
                        f.write(raw_html)
                    webbrowser.open(f"file://{temp_path}")
                except Exception as e:
                    st.error(f"שגיאה בהפקת הדוח: {e}")

    max_policy_years = max(1, 80 - entry_age)
    y70 = max(0, 70 - entry_age)
    y75 = max(0, 75 - entry_age)

    horizon_map = {"חודש 1": 1 / 12, "3 שנים": 3, "5 שנים": 5, "10 שנים": 10, "15 שנים": 15}
    if y70 > 0 and y70 < max_policy_years: horizon_map[f"עד גיל 70 ({y70} שנ')"] = y70
    if y75 > 0 and y75 < max_policy_years: horizon_map[f"עד גיל 75 ({y75} שנ')"] = y75
    horizon_map[f"עד גיל 80 (מצטבר מלא - {max_policy_years} שנ')"] = max_policy_years

    selected_horizon = st.radio("טווח זמן לבדיקה:", options=list(horizon_map.keys()), index=4, horizontal=True)
    h_key = horizon_map[selected_horizon]

    for plan in plans:
        lookup_key = 1 if h_key == 1 / 12 else int(h_key)
        current_monthly_rate = c['m_rates'][plan['key']].get(lookup_key, c['m_rates'][plan['key']].get(1, 0))

        if h_key == 1 / 12:
            total_cost = current_monthly_rate
            big_suffix = "/ חודש 1"
            subtext = "פרמיה התחלתית"
        else:
            total_cost = c['cum_res'].get(lookup_key, {}).get(plan['key'], current_monthly_rate * 12 * h_key)
            big_suffix = f"/ סה״כ ל-{selected_horizon}"
            subtext = f"פרמיה חודשית (בשנה {lookup_key}): ₪{current_monthly_rate:,.1f}"

        st.markdown(f"""
        <div class='deal-card'>
            <div><span class='pill-badge'>{plan['badge']}</span></div>
            <div style='text-align: center;'>
                <div class='deal-price'>₪{total_cost:,.0f} <span style='font-size:16px; color:#64748B;'>{big_suffix}</span></div>
                <div class='deal-subprice'>{subtext}</div>
            </div>
            <div style='text-align: left;'>
                <div style='font-size: 19px; font-weight: 800; color: #1E293B;'>{plan['name']}</div>
                <div style='font-size: 13px; color: #64748B;'>{plan['company']}</div>
            </div>
        </div>
        """, unsafe_allow_html=True)


# ==========================================
# PHASE 2: RESULTS SCREEN - CRITICAL ILLNESS
# ==========================================
elif st.session_state.view_mode == 'results_ci':
    c = st.session_state.calc_data
    entry_age = c['age']
    coverage = c['coverage']

    plans = []
    if "Existing Policy" in c['m_rates']:
        plans.append({"name": "הפוליסה הנוכחית", "company": "נתונים מתוך דוח מצורף", "badge": "מצב קיים",
                      "key": "Existing Policy"})

    plans.append({"name": "איילון בשביל החוסן - מבצע מיוחד (40%)", "company": "איילון חברה לביטוח",
                  "badge": "40% הנחה ל-10 שנים", "key": "Ayalon CI - 40%"})

    if coverage < 250000:
        plans.append(
            {"name": "איילון בשביל החוסן - רגיל (30%)", "company": "איילון חברה לביטוח", "badge": "30% הנחה ל-10 שנים",
             "key": "Ayalon CI - 30%"})
    else:
        plans.append(
            {"name": "איילון בשביל החוסן - מורחב (35%)", "company": "איילון חברה לביטוח", "badge": "35% הנחה ל-10 שנים",
             "key": "Ayalon CI - 35%"})

    plans.append({"name": "תעריף ללא הנחות", "company": "איילון חברה לביטוח", "badge": "מתעדכן כל שנה לפי גיל",
                  "key": "Ayalon CI - Base"})

    top_col1, top_col2, top_col3 = st.columns([1, 2, 1.2])
    with top_col1:
        if st.button("✏️ חזרה לעריכה", use_container_width=True):
            st.session_state.view_mode = 'input'
            st.rerun()

    with top_col2:
        st.markdown(
            f"<div style='text-align: center; font-weight:700;'>🩺 כיסוי מחלות קשות: <b>₪{c['coverage']:,}</b> &nbsp;|&nbsp; 👤 מבוטח: <b>{c.get('client_name', '')} (גיל {entry_age})</b></div>",
            unsafe_allow_html=True)

    with top_col3:
        if st.button("📄 הצג דוח מלא", type="primary", use_container_width=True):
            with st.spinner("מכין דוח מחלות קשות..."):
                try:
                    raw_html = generate_ci_html_report(c, plans, report_title="דוח השוואת ביטוח מחלות קשות", max_expiry_age=75)
                    fd, temp_path = tempfile.mkstemp(suffix=".html")
                    with os.fdopen(fd, 'wb') as f: f.write(raw_html)
                    webbrowser.open(f"file://{temp_path}")
                except Exception as e:
                    st.error(f"שגיאה בהפקת הדוח: {e}")

    max_policy_years = max(1, 75 - entry_age)
    y70 = max(0, 70 - entry_age)

    horizon_map = {"חודש 1": 1 / 12, "10 שנים": 10, "15 שנים": 15}
    if y70 > 0 and y70 < max_policy_years: horizon_map[f"עד גיל 70 ({y70} שנ')"] = y70
    horizon_map[f"עד גיל 75 (מצטבר מלא - {max_policy_years} שנ')"] = max_policy_years

    selected_horizon = st.radio("טווח זמן לבדיקה:", options=list(horizon_map.keys()), index=4, horizontal=True)
    h_key = horizon_map[selected_horizon]

    for plan in plans:
        lookup_key = 1 if h_key == 1 / 12 else int(h_key)
        current_monthly_rate = c['m_rates'][plan['key']].get(lookup_key, c['m_rates'][plan['key']].get(1, 0))

        if h_key == 1 / 12:
            total_cost = current_monthly_rate
            big_suffix = "/ חודש 1"
            subtext = "פרמיה התחלתית"
        else:
            total_cost = c['cum_res'].get(lookup_key, {}).get(plan['key'], current_monthly_rate * 12 * h_key)
            big_suffix = f"/ סה״כ ל-{selected_horizon}"
            subtext = f"פרמיה חודשית (בשנה {lookup_key}): ₪{current_monthly_rate:,.1f}"

        st.markdown(f"""
        <div class='deal-card'>
            <div><span class='pill-badge'>{plan['badge']}</span></div>
            <div style='text-align: center;'>
                <div class='deal-price'>₪{total_cost:,.0f} <span style='font-size:16px; color:#64748B;'>{big_suffix}</span></div>
                <div class='deal-subprice'>{subtext}</div>
            </div>
            <div style='text-align: left;'>
                <div style='font-size: 19px; font-weight: 800; color: #1E293B;'>{plan['name']}</div>
                <div style='font-size: 13px; color: #64748B;'>{plan['company']}</div>
            </div>
        </div>
        """, unsafe_allow_html=True)