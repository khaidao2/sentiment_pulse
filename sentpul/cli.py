import os
import yaml
import json
import click
from sentpul.apicurio_client import register_schema
from sentpul.validator import load_avro_schema, validate_record
from sentpul.renderer import render_template, write_rendered_file

def load_yaml_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

@click.group()
def main():
    """Sentiment Pulse Data Contract CLI Tool (sent-gen)"""
    pass

@main.command()
@click.option("--config-dir", default="data-contracts/infra", help="Directory containing contract configs")
def register(config_dir):
    """Register all Avro schemas from configs in Apicurio Registry."""
    if not os.path.exists(config_dir):
        click.echo(f"Config directory {config_dir} does not exist.")
        return
        
    for filename in os.listdir(config_dir):
        if filename.endswith(".yaml") or filename.endswith(".yml"):
            path = os.path.join(config_dir, filename)
            try:
                config = load_yaml_config(path)
                schema_path = config["schema_file"]
                schema = load_avro_schema(schema_path)
                
                apicurio_cfg = config.get("apicurio", {})
                group_id = apicurio_cfg.get("group_id", "default")
                artifact_id = apicurio_cfg.get("artifact_id", config["name"])
                
                click.echo(f"Registering schema '{artifact_id}'...")
                register_schema(group_id, artifact_id, schema)
            except Exception as e:
                click.echo(f"Error processing {filename}: {e}")

@main.command()
@click.option("--config-dir", default="data-contracts/infra", help="Directory containing contract configs")
def render(config_dir):
    """Render Kafka Producers and Airflow DAGs from templates."""
    if not os.path.exists(config_dir):
        click.echo(f"Config directory {config_dir} does not exist.")
        return
        
    for filename in os.listdir(config_dir):
        if filename.endswith(".yaml") or filename.endswith(".yml"):
            path = os.path.join(config_dir, filename)
            try:
                config = load_yaml_config(path)
                schema_path = config["schema_file"]
                schema = load_avro_schema(schema_path)
                
                # Context passed to template
                context = {
                    "name": config["name"],
                    "class_name": "".join([part.capitalize() for part in config["name"].split("_")]),
                    "kafka_topic": config["kafka"]["topic"],
                    "kafka_bootstrap_servers": config["kafka"]["bootstrap_servers"],
                    "airflow_schedule": config["airflow"]["schedule"],
                    "schema_json_str": json.dumps(schema, indent=2),
                    "sink_target": config["sink"]["target"],
                    "batch_size": config["sink"].get("batch_size", 1000),
                    "batch_timeout_ms": config["sink"].get("batch_timeout_ms", 5000),
                    "clickhouse": config["sink"].get("clickhouse", {})
                }
                
                # Render and write Producer
                producer_content = render_template("producer.py.jinja", context)
                write_rendered_file(config["producer"]["output_path"], producer_content)
                
                # Render and write Sink
                sink_template = f"{config['sink']['target']}_sink.py.jinja"
                sink_content = render_template(sink_template, context)
                write_rendered_file(config["sink"]["output_path"], sink_content)
                
                # Render and write Airflow DAG
                dag_content = render_template("dag.py.jinja", context)
                write_rendered_file(config["airflow"]["output_dag_path"], dag_content)
                
            except Exception as e:
                click.echo(f"Error rendering {filename}: {e}")

@main.command()
@click.option("--contract", required=True, help="Path to contract YAML file")
@click.option("--data", required=True, help="Path to JSON data file to validate")
def validate(contract, data):
    """Validate a JSON data record against a contract's Avro schema."""
    try:
        config = load_yaml_config(contract)
        schema_path = config["schema_file"]
        schema = load_avro_schema(schema_path)
        
        with open(data, "r", encoding="utf-8") as f:
            record = json.load(f)
            
        # validate record
        if isinstance(record, list):
            click.echo(f"Validating list of {len(record)} records...")
            for idx, r in enumerate(record):
                validate_record(r, schema)
            click.echo("All records are VALID!")
        else:
            click.echo("Validating single record...")
            validate_record(record, schema)
            click.echo("Record is VALID!")
            
    except Exception as e:
        click.echo(f"VALIDATION FAILED: {e}")
        raise click.Abort()

@main.command()
@click.option("--config-dir", default="data-contracts/infra", help="Directory containing contract configs")
def run_all(config_dir):
    """Run both registration and code rendering."""
    click.echo("=== STEP 1: Registering Schemas to Apicurio ===")
    ctx = click.get_current_context()
    ctx.invoke(register, config_dir=config_dir)
    
    click.echo("\n=== STEP 2: Rendering Producers and DAGs ===")
    ctx.invoke(render, config_dir=config_dir)

if __name__ == "__main__":
    main()
