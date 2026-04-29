import os
import ast

base_dir = "/Users/enkidu/Pyproject/StillAlive/Server/apps/characters"
tasks_file = os.path.join(base_dir, "tasks.py")
services_dir = os.path.join(base_dir, "services")

os.makedirs(services_dir, exist_ok=True)
with open(os.path.join(services_dir, "__init__.py"), "w") as f:
    pass

with open(tasks_file, "r") as f:
    source_lines = f.readlines()

with open(tasks_file, "r") as f:
    source_code = f.read()

tree = ast.parse(source_code)

functions = {}
for node in tree.body:
    if isinstance(node, ast.FunctionDef):
        # To include decorators
        start_line = node.decorator_list[0].lineno if node.decorator_list else node.lineno
        end_line = node.end_lineno
        functions[node.name] = "".join(source_lines[start_line-1:end_line]) + "\n"

# Check if we got them
aggregate_status_data = functions.get("aggregate_status_data", "")
extract_text_from_anthropic_response = functions.get("extract_text_from_anthropic_response", "")
_clean_markdown_wrapper = functions.get("_clean_markdown_wrapper", "")
_build_data_section = functions.get("_build_data_section", "")
analyze_with_llm = functions.get("analyze_with_llm", "")
_get_raw_data_summary = functions.get("_get_raw_data_summary", "")
update_system_persona = functions.get("update_system_persona", "")

send_will_email = functions.get("send_will_email", "")
check_wills = functions.get("check_wills", "")
generate_daily_reports = functions.get("generate_daily_reports", "")

# Write data_service.py
data_service_code = f"""import logging
from django.utils import timezone
from datetime import timedelta, datetime
from collections import defaultdict, Counter
from apps.characters.models import CharacterStatus

logger = logging.getLogger(__name__)

{aggregate_status_data}
"""
with open(os.path.join(services_dir, "data_service.py"), "w") as f:
    f.write(data_service_code)

# Write llm_service.py
llm_service_code = f"""import json
import logging
from django.utils import timezone
from django.conf import settings

logger = logging.getLogger(__name__)

{extract_text_from_anthropic_response}

{_clean_markdown_wrapper}

{_build_data_section}

{analyze_with_llm}
"""
with open(os.path.join(services_dir, "llm_service.py"), "w") as f:
    f.write(llm_service_code)

# Write persona_service.py
persona_service_code = f"""import json
import logging
from django.utils import timezone
from django.conf import settings
from apps.characters.models import DailyReport, PersonaHistory
from .llm_service import extract_text_from_anthropic_response

logger = logging.getLogger(__name__)

{_get_raw_data_summary}

{update_system_persona}
"""
with open(os.path.join(services_dir, "persona_service.py"), "w") as f:
    f.write(persona_service_code)

# Write new tasks.py
tasks_code = f"""from celery import shared_task
from django.utils import timezone
from datetime import timedelta, datetime
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.conf import settings
from .models import WillConfig, CharacterStatus, DailyReportConfig, DailyReport
import logging

from .services.data_service import aggregate_status_data
from .services.llm_service import analyze_with_llm
from .services.persona_service import update_system_persona

logger = logging.getLogger(__name__)

{send_will_email}

{check_wills}

{generate_daily_reports}
"""
with open(tasks_file, "w") as f:
    f.write(tasks_code)

print("Refactoring complete.")
