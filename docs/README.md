# Data Contracts System User Guide (`sent-gen` CLI)

The Data Contracts system of the **Sentiment Pulse** project helps standardize and validate incoming data structures using Avro Schema, automatically register schemas to the **Apicurio Registry**, and auto-generate (render) source code for **Kafka Producers**, **Kafka Sinks (Consumers)**, and **Airflow DAGs**.

---

## 1. Starting the Local Test Environment (Redpanda)

To facilitate local Kafka message production and consumption testing without a heavy ZooKeeper setup, the system uses **Redpanda** and **Redpanda Console**.

### Start services:
At the project root directory (where `docker-compose.yml` is located), run the command:
```bash
docker compose up -d
```

### Local service ports:
- **Redpanda Broker (Kafka API)**: `localhost:9092` (used by Producers to send data).
- **Redpanda Console (Web UI)**: [http://localhost:8080](http://localhost:8080) (a very intuitive web interface to inspect Topics, Consumer Groups, and browse message contents).

---

## 2. Installing the `sent-gen` CLI Tool

The system provides a command-line tool (CLI) written in Python called `sent-gen`.

### Development installation instructions (Editable Mode):
Run the following command at the root of the project:
```bash
pip install -e .
```
This command installs the `sentpul` module in editable mode and registers a global `sent-gen` command shortcut on your development machine.

---

## 3. Using the `sent-gen` CLI

After installation, you can run `sent-gen --help` to view the list of available commands:

### A. Auto-Generate Source Code (Render)
Reads YAML contract configurations from the `data-contracts/infra/` directory and renders corresponding Python files for the Kafka Producer, Kafka Sink (Consumer), and Airflow DAG.
```bash
sent-gen render
```
*Outputs:*
- Creates a Kafka Producer class at: `api_crawler/<source_name>/producer.py`
- Creates a Kafka Sink (Consumer) class at: `api_crawler/<source_name>/sink.py`
- Creates an Airflow DAG file at: `dags/<source_name>_dag.py`

### B. Register Schema to Apicurio Registry
Reads configurations and calls the registration API to publish Avro Schema (`.avsc`) files to the Apicurio Registry server (ensure the `APICURIO_REGISTRY_URL` environment variable points to the correct URL).
```bash
sent-gen register
```
*Note:* By default, the CLI targets `http://apicurio.kafka.svc.cluster.local:80` (inside the K3s cluster). You can override this using an environment variable when running locally:
```bash
export APICURIO_REGISTRY_URL="https://apicurio.sentpul.click"
sent-gen register
```

> **Heads-up — runtime registry interaction:** running `sent-gen register` is convenience/bootstrapping only. The **generated Producer and Sink classes also talk to Apicurio Registry at runtime** (see section 4), so wherever they're deployed, `APICURIO_REGISTRY_URL` must resolve to a reachable Apicurio instance — otherwise the Producer fails to start (it needs a `globalId` to frame messages) and the Sink falls back to its locally embedded schema for decoding.

### C. Run All (Run All)
Runs both schema registration to Apicurio and automatic source code generation:
```bash
sent-gen run-all
```

### D. Validate JSON Data
Validates whether the JSON data (crawler/API inputs) conforms to the Avro Schema declared in the contract:
```bash
# Validate a JSON file containing a single record or a list of records
sent-gen validate --contract data-contracts/infra/news.yaml --data path/to/your-data.json
```
If the data is valid, the CLI will output `Record is VALID!`. If it fails, the CLI will output a detailed validation error indicating which fields are incorrect.

---

## 4. Using the Generated Producer and Sink Python Classes

The generated files are complete, importable Python classes, allowing you to easily integrate them into other Python scripts in your project.

### A. Using the Producer to send data to Kafka (Auto-Validation & Registry-aware Avro Serialization):
On `__init__`, the Producer calls Apicurio Registry to fetch its schema's `globalId` — registering the schema there first if it isn't present yet — and frames every message with the standard wire-format header (`magic byte (0x0)` + 4-byte big-endian `globalId`) followed by the raw Avro payload. This requires `APICURIO_REGISTRY_URL` (or its in-cluster default) to be reachable when the Producer starts up.
```python
from api_crawler.news.producer import NewsProducer

# Initialize the Producer (defaults to bootstrap_servers from the YAML config).
# This also calls Apicurio Registry to get-or-register the schema and resolve its globalId.
producer = NewsProducer(bootstrap_servers="localhost:9092")

# News data to send
data = {
    "id": "123e4567-e89b-12d3-a456-426614174000",
    "title": "Market News Update",
    "content": "Stock prices are fluctuating heavily...",
    "source": "Cafef",
    "published_at": "2026-05-29T22:00:00Z",
    "created_at": "2026-05-29T22:01:00Z"
}

# Send data: the class validates it against the Avro Schema, serializes it to
# Avro binary framed with the magic-byte + schema globalId header, and pushes it to Kafka
producer.send(data)
```

### B. Using the Sink to consume data from Kafka and insert into ClickHouse:
For each message, the Sink reads the schema `globalId` from the wire-format header and resolves the matching Avro schema via `resolve_schema()` — fetching it from Apicurio Registry on first use and **caching it by `globalId`** so repeated lookups are free. This lets the Sink correctly decode messages produced under older or newer schema versions than the one baked into it (no need to redeploy Producer and Sink in lockstep). If Apicurio is unreachable, it falls back to its locally embedded schema with a warning.

You can import the class for integration or run the Sink script file directly:
```bash
# Run the Sink script independently to constantly listen and forward data to ClickHouse
python api_crawler/news/sink.py
```
Or import it in code:
```python
from api_crawler.news.sink import NewsSink

# Initialize the Sink
sink = NewsSink(bootstrap_servers="localhost:9092")

# Start the consuming loop from Kafka, automatically validate/process, and push to ClickHouse
sink.start_consuming()
```

---

## 5. Process for Adding a New Data Source

To extend the system with a new data source (e.g., `social_media`):

1. **Create Avro Schema**: Create the file `data-contracts/schemas/social_media.avsc` defining data fields according to the Avro standard.
2. **Declare Contract**: Create the configuration file `data-contracts/infra/social_media.yaml` specifying the schema file path, Kafka topic, Airflow schedule, and desired render output paths.
3. **Run the Generator**:
   ```bash
   sent-gen run-all
   ```
4. The system will automatically create the Producer file at `api_crawler/social_media/producer.py`, the Sink at `api_crawler/social_media/sink.py`, and the DAG at `dags/social_media_dag.py` completely automatically!
