import glob
import re
import sys
import subprocess
import os
import json

pattern = '\=|~|>|<| |\n|#|\['  # noqa: W605


def get_requirements_from_file(path):
    """Takes a requirements file path and extracts only the package names from it"""

    with open(path, 'r') as main_f:
        reqs = [
            re.split(pattern, line)[0]
            for line in main_f.readlines()
            if re.split(pattern, line)[0]
        ]
    return reqs


MAIN_REQS_PATH = "requirements/requirements.txt"
DEV_REQS_PATH = "requirements/requirements-dev.txt"
TEST_REQS_PATH = "requirements/requirements-test.txt"
GRPC_REQS_PATH = "requirements/requirements-grpc.txt"
DOCKER_REQS_PATH = "docker/handler_discovery/requirements.txt"

HANDLER_REQS_PATHS = list(
    set(glob.glob("**/requirements*.txt", recursive=True))
    - set(glob.glob("requirements/requirements*.txt"))
)

MAIN_EXCLUDE_PATHS = ["mindsdb/integrations/handlers/.*_handler", "pryproject.toml"]

# Torch.multiprocessing is imported in a 'try'. Falls back to multiprocessing so we dont NEED it.
# Psycopg2 is needed in core codebase for sqlalchemy.
# Hierarchicalforecast is an optional dep of neural/statsforecast
MAIN_RULE_IGNORES = {
    "DEP003": ["torch"],
    # Ignore Langhchain since the requirements check will still fail even if it's conditionally imported for certain features.
    "DEP001": ["torch"],
    "DEP002": ["psycopg2-binary"],
}

# THe following packages need exceptions because they are optional deps of some other packages. e.g. langchain CAN use openai
# (pysqlite3 is imported in an unusual way in the chromadb handler and needs to be excluded too)
# pypdf and openpyxl are optional deps of langchain, that are used for the file handler
OPTIONAL_HANDLER_DEPS = ["pysqlite3", "torch", "openai", "tiktoken", "wikipedia", "anthropic", "pypdf", "openpyxl"]

# List of rules we can ignore for specific packages
# Here we ignore any packages in the main requirements.txt for "listed but not used" errors, because they will be used for the core code but not necessarily in a given handler
MAIN_REQUIREMENTS_DEPS = get_requirements_from_file(MAIN_REQS_PATH) + get_requirements_from_file(
    TEST_REQS_PATH) + get_requirements_from_file(GRPC_REQS_PATH)

BYOM_HANLDER_DEPS = ["pyarrow"]

HANDLER_RULE_IGNORES = {
    "DEP002": OPTIONAL_HANDLER_DEPS + MAIN_REQUIREMENTS_DEPS + BYOM_HANLDER_DEPS,
    "DEP001": ["tests"]  # 'tests' is the mindsdb tests folder in the repo root
}

PACKAGE_NAME_MAP = {
    "scylla-driver": ["cassandra"],
    "mysql-connector-python": ["mysql"],
    "snowflake-connector-python": ["snowflake"],
    "snowflake-sqlalchemy": ["snowflake"],
    "auto-sklearn": ["autosklearn"],
    "google-cloud-aiplatform": ["google"],
    "google-cloud-bigquery": ["google"],
    "google-cloud-spanner": ["google"],
    "google-auth-httplib2": ["google"],
    "google-generativeai": ["google"],
    "google-analytics-admin": ["google"],
    "protobuf": ["google"],
    "google-api-python-client": ["googleapiclient"],
    "binance-connector": ["binance"],
    "pysqlite3": ["pysqlite3"],
    "sqlalchemy-spanner": ["sqlalchemy"],
    "atlassian-python-api": ["atlassian"],
    "databricks-sql-connector": ["databricks"],
    "elasticsearch-dbapi": ["es"],
    "pygithub": ["github"],
    "python-gitlab": ["gitlab"],
    "impyla": ["impala"],
    "IfxPy": ["IfxPyDbi"],
    "salesforce-merlion": ["merlion"],
    "newsapi-python": ["newsapi"],
    "pinecone-client": ["pinecone"],
    "plaid-python": ["plaid"],
    "faiss-cpu": ["faiss"],
    "writerai": ["writer"],
    "rocketchat_API": ["rocketchat_API"],
    "ShopifyAPI": ["shopify"],
    "solace-pubsubplus": ["solace"],
    "taospy": ["taosrest"],
    "weaviate-client": ["weaviate"],
    "pymupdf": ["fitz"],
    "ibm-db": ["ibm_db_dbi"],
    "python-dateutil": ["dateutil"],
    "grpcio": ["grpc"],
    "sqlalchemy-redshift": ["redshift_sqlalchemy"],
    "sqlalchemy-vertica-python": ["sqla_vertica_python"],
    "grpcio-tools": ["grpc"],
    "psycopg2-b
# ... [truncated] ...
append(line.split("mindsdb/integrations/handlers/")[1].split("/")[0])  # just return
                        # the handler name

        return entries

    for handler_dir in glob.glob("mindsdb/integrations/handlers/*/"):
        handler_name = handler_dir.split("/")[-2].split("_handler")[0]

        # regex for finding imports of other handlers like "from mindsdb.integrations.handlers.file_handler import FileHandler"
        # excludes the current handler importing parts of itself
        import_pattern = re.compile(
            f"(?:\s|^)(?:from|import) mindsdb\.integrations\.handlers\.(?!{handler_name})\w+_handler")  # noqa: W605

        # requirements entries for this handler that point to another handler's requirements file
        required_handlers = get_relative_requirements(
            [file for file in HANDLER_REQS_PATHS if file.startswith(handler_dir)])

        all_imported_handlers = []

        # for every python file in this handler's code
        for file in glob.glob(f"{handler_dir}/**/*.py", recursive=True):
            errors = []

            # find all the imports of handlers
            with open(file, "r") as f:
                file_content = f.read()
                relative_imported_handlers = [match.strip() for match in
                                              re.findall(relative_import_pattern, file_content)]
                handler_import_lines = [match.strip() for match in re.findall(import_pattern, file_content)]

            imported_handlers = {line: line.split("_handler")[0].split(".")[-1] + "_handler" for line in
                                 handler_import_lines}
            all_imported_handlers += imported_handlers.values()

            # Report on relative imports (like "from ..file_handler import FileHandler")
            for line in relative_imported_handlers:
                errors.append(f"{line} <- Relative import of handler. Use absolute import instead")

            # Report on imports of other handlers that are missing a corresponding requirements.txt entry
            for line, imported_handler_name in imported_handlers.items():
                if imported_handler_name not in required_handlers:
                    errors.append(
                        f"{line} <- {imported_handler_name} not in handler requirements.txt. Add it like: \"-r mindsdb/integrations/handlers/{imported_handler_name}/requirements.txt\"")

            # Print all the errors for this .py file
            print_errors(file, errors)

        # Report on requirements.txt entries that point to a handler that isn't used
        requirements_errors = [required_handler_name for required_handler_name in required_handlers if
                               required_handler_name not in all_imported_handlers]
        print_errors(handler_dir, requirements_errors)


def check_requirements_imports():
    """
    Use deptry to find issues with dependencies.

    Runs deptry on the core codebase (excluding handlers) + the main requirements.txt file.
    Then runs it on each handler codebase and requirements.txt individually.
    """

    # Run against the main codebase
    errors = run_deptry(
        ','.join([MAIN_REQS_PATH, GRPC_REQS_PATH, DOCKER_REQS_PATH]),
        get_ignores_str(MAIN_RULE_IGNORES),
        ".",
        f"--extend-exclude \"{'|'.join(MAIN_EXCLUDE_PATHS)}\"",
    )
    print_errors(MAIN_REQS_PATH, errors)

    # Run on each handler
    for file in HANDLER_REQS_PATHS:
        errors = run_deptry(
            f"{file},{MAIN_REQS_PATH},{TEST_REQS_PATH}",
            get_ignores_str(HANDLER_RULE_IGNORES),
            os.path.dirname(file),
        )
        print_errors(file, errors)


print("--- Checking requirements files for duplicates ---")
check_for_requirements_duplicates()
print()

print("--- Checking that requirements match imports ---")
check_requirements_imports()
print()

print("--- Checking handlers that require other handlers ---")
check_relative_reqs()

sys.exit(0 if success else 1)

