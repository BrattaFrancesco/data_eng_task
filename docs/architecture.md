# Architecture

This document describes the architecture of the streaming feature pipeline and ML scoring system implemented in this repository. The system simulates a real-time data platform using Kafka-like event ingestion, DynamoDB-style state storage, and an XGBoost model for customer value prediction. The design emphasizes **replayability, idempotency, and reproducibility**.

---

## 1. Kafka topics & message structure

### Topic names

The system is conceptually organized around one Kafka topic:

* **`customer-events`**
  Raw transactional events emitted by upstream producers.

> In the current implementation, Kafka is simulated via a Python generator, that create events randomly using certain parameters. Afterwards, there is a function that simulates a tumbling window of 5 minutes, grouping those event by minutes and customer. In this way, the script is able to process events that are part of the same window (5 minutes long) by customer id, respecting the message key logic. It is a simulation of a window function.

---

### Message schema

**`customer-events` message schema (JSON):**

```json
{
  "event_id": "uuid-or-unique-string",
  "customer_id": "C123",
  "event_time": "2025-01-01T10:05:00Z",
  "event_type": "transaction",
  "amount": 120.50
}
```

### Message key strategy

* Kafka **message key**: `customer_id`
* This ensures:

  * Events for the same customer are routed to the same partition
  * Per-customer ordering guarantees within a partition
  * Efficient stateful processing downstream

---

## 2. Ordering, duplicates, and delivery semantics

### Delivery guarantees

* The system assumes **at-least-once delivery** semantics.
* Events may be:

  * duplicated
  * delivered out of order
* This reflects real-world Kafka behavior under retries or failures.

---

### Duplicate handling

* Each event contains a globally unique `event_id`.
* A **deduplication mechanism** tracks processed `event_id`s.
* Events with already-seen IDs are ignored.
* This makes event processing **idempotent**.

---

### Ordering strategy

* Events are **partitioned by `customer_id`**.
* Within a partition, Kafka guarantees ordering.
* Feature computation uses **event time (`event_time`)**, not processing time.
* Late or out-of-order events are still incorporated correctly into rolling windows.

---

## 3. State storage

### DynamoDB table design

The system uses a DynamoDB-style key-value store (simulated in memory).

**Table: `customer_feature_state`**

| Attribute | Role                                      |
| --------- | ----------------------------------------- |
| `PK`      | `CUSTOMER#{customer_id}` (partition key)  |
| `SK`      | `STATE` or time-based sort key (optional) |

**Stored state includes:**

* Rolling feature aggregates (e.g. 30-day windows)
* Last processed event timestamp
* Optional version metadata

Example item:

```json
{
  "PK": "CUSTOMER#C123",
  "features": {
    "total_txn_30d": 12,
    "total_amount_30d": 842.5,
    "avg_amount_30d": 70.21
  },
  "last_event_time": "2025-01-01T10:05:00Z",
  "version": 17
}
```

---

### Concurrent updates

* Updates are logically serialized by partitioning on `customer_id`.
* In a real DynamoDB setup, concurrency would be handled using:

  * **optimistic locking**
  * a `version` attribute with conditional writes (`ConditionExpression`)
* This prevents lost updates when multiple consumers process the same customer concurrently.

---

## 4. Model versioning & deployment

### Model artifact storage

* Trained models are saved as serialized artifacts (e.g. `high_value_customer_model.json`).
* Artifacts are stored alongside the code or in a shared artifact store (e.g. S3 in production).

---

### Versioning strategy

* Each trained model is versioned explicitly:

  * filename includes version or timestamp
  * metadata records training date and feature schema
* Example:

  ```
  high_value_customer_model_v1.json
  high_value_customer_model_v2.json
  ```

---

### Rollout & rollback

* New models are deployed by:

  * updating the model artifact reference
  * restarting the scoring service/container
* Rollback is achieved by:

  * switching back to a previous model artifact
* No state migration is required because features are computed independently of the model.

---

## 5. Replay / backfill

### Rebuilding features from event history

* The **event log is the source of truth**.
* Feature state can be rebuilt by:

  1. Clearing the state store
  2. Replaying all events from the beginning
  3. Recomputing features deterministically

This supports:

* disaster recovery
* feature logic changes
* model retraining with historical data

---

### Large-scale backfills

For large-scale backfills in production, the system would:

* Read historical events from durable storage (Kafka with long retention or object storage like S3)
* Process events in parallel using:

  * batch jobs (e.g. Spark / Flink)
  * or partitioned consumers
* Write recomputed state back to DynamoDB using throttled, idempotent writes
* Optionally backfill in time slices to limit blast radius
