# Hướng dẫn Sử dụng Hệ thống Data Contracts (`sent-gen` CLI)

Hệ thống Data Contracts của dự án **Sentiment Pulse** giúp chuẩn hóa, kiểm tra (validate) cấu trúc dữ liệu đầu vào bằng Avro Schema, đồng thời tự động đăng ký schema lên **Apicurio Registry** và tự động sinh mã nguồn (render) cho **Kafka Producers** và **Airflow DAGs**.

---

## 1. Khởi chạy Môi trường Test Local (Redpanda)

Để phục vụ kiểm thử gửi nhận message Kafka local mà không cần setup ZooKeeper cồng kềnh, hệ thống sử dụng **Redpanda** và **Redpanda Console**.

### Khởi chạy dịch vụ:
Tại thư mục gốc của project (nơi chứa file `docker-compose.yml`), chạy lệnh:
```bash
docker compose up -d
```

### Các cổng dịch vụ local:
- **Redpanda Broker (Kafka API)**: `localhost:9092` (dùng để các Producer gửi dữ liệu vào).
- **Redpanda Console (Web UI)**: [http://localhost:8080](http://localhost:8080) (giao diện xem Topics, Consumer Groups, và duyệt nội dung các messages rất trực quan).

---

## 2. Cài đặt CLI Tool `sent-gen`

Hệ thống cung cấp một công cụ dòng lệnh (CLI) viết bằng Python tên là `sent-gen`.

### Hướng dẫn cài đặt chế độ phát triển (Editable Mode):
Chạy lệnh sau tại thư mục gốc của project:
```bash
pip install -e .
```
Lệnh này sẽ cài đặt module `sentpul` và tạo liên kết lệnh `sent-gen` toàn cục trên máy dev của bạn.

---

## 3. Hướng dẫn Sử dụng `sent-gen` CLI

Sau khi cài đặt, bạn có thể gõ `sent-gen --help` để xem danh sách các câu lệnh có sẵn:

### A. Sinh mã nguồn tự động (Render)
Đọc cấu hình contract YAML từ thư mục `data-contracts/infra/` và render ra các file Python cho Kafka Producer và Airflow DAG tương ứng.
```bash
sent-gen render
```
*Kết quả:*
- Tạo file Kafka Producer tại: `api_crawler/<source_name>/producer.py`
- Tạo file Airflow DAG tại: `dags/<source_name>_dag.py`

### B. Đăng ký Schema lên Apicurio Registry
Đọc cấu hình và gửi API đăng ký các file Avro Schema (`.avsc`) lên server Apicurio Registry (chỉ cần đảm bảo biến môi trường `APICURIO_REGISTRY_URL` trỏ đúng địa chỉ).
```bash
sent-gen register
```
*Lưu ý:* Mặc định CLI sẽ gọi tới `http://apicurio.kafka.svc.cluster.local:80` (trong cụm K3s). Bạn có thể override bằng biến môi trường khi chạy local:
```bash
export APICURIO_REGISTRY_URL="https://apicurio.sentpul.click"
sent-gen register
```

### C. Chạy toàn bộ (Run All)
Chạy cả hai bước đăng ký schema lên Apicurio và sinh mã nguồn tự động:
```bash
sent-gen run-all
```

### D. Kiểm tra (Validate) dữ liệu JSON
Kiểm tra xem dữ liệu JSON (đầu vào của crawler/API) có khớp và hợp lệ với Avro Schema được khai báo trong contract hay không:
```bash
# Validate một file JSON chứa 1 record hoặc danh sách records
sent-gen validate --contract data-contracts/infra/news.yaml --data path/to/your-data.json
```
Nếu dữ liệu hợp lệ, CLI sẽ in ra thông báo `Record is VALID!`. Nếu lỗi, CLI sẽ báo lỗi chi tiết cấu trúc sai ở trường nào.

---

## 4. Quy trình Thêm một Nguồn Dữ liệu Mới (Data Source)

Để mở rộng hệ thống thêm một nguồn dữ liệu mới (ví dụ: `social_media`):

1. **Tạo Avro Schema**: Tạo file `data-contracts/schemas/social_media.avsc` định nghĩa các trường dữ liệu theo chuẩn Avro.
2. **Khai báo Contract**: Tạo file cấu hình `data-contracts/infra/social_media.yaml` chỉ ra đường dẫn file schema, topic Kafka, lịch chạy Airflow, và đường dẫn mong muốn render output.
3. **Chạy Generator**:
   ```bash
   sent-gen run-all
   ```
4. Hệ thống sẽ tự động tạo file Producer tại `api_crawler/social_media/producer.py` và DAG tại `dags/social_media_dag.py` hoàn toàn tự động!
