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
* This ensures taht events for the same customer are routed to the same partition

---

## 2. Ordering, duplicates, and delivery semantics

### Delivery guarantees

* The system assumes **at-least-once delivery** semantics. In this case we don't have networks that can cause lose of data, so the data is always processed at least once.
* But to simulate the consequences of lost of data, even though it doesn't actually happens, events may be:
  * duplicated
  * delivered out of order
* This reflects real-world Kafka behavior under retries or failures.

---

### Duplicate handling

* Each event contains a globally unique `event_id`.
* A **deduplication mechanism** tracks processed `event_id`, using a idempotent table in the in-memory DynamoDB (that in this case is a simple python set recording all the events that occourred).
* Events with already-seen IDs are ignored.
* This makes event processing **idempotent**.

---

### Ordering strategy

* Events are **partitioned by `customer_id`** and they are processed in a window of 5 minutes. In this case it doesn't really matter the ordering of the events, because an event arriving at 10:05 followed by an event at 10:02 in the same day do not change our final result.
* The partition per `customer_id` ensure us a sort of aggregation per customer done by the window function.

---

## 3. State storage

### DynamoDB tables design

The system uses a DynamoDB-style key-value store (simulated in memory).

#### Table: `balances`

> **Note** 
> In the current implementation these informations are stored in one single table, as you can see in the item example. This is a design choice to simplify the development, since the architectural part was not covered in the assesment cretarias. In a real world scenario, the daily aggratates and the features should be included or not in the main table depending on the system requirements. It is obvious that the implemented case it is faster, since it doesn't require any join between tables, but is not scalable. In the schema I will show what conceptually makes more sense to me, but then as I said, in reality the implementation can change based on the system requirements.

##### Customer
| Attribute    | Type | Description |
|-------------|------|-------------|
| `customer_id` | String `PK` | Unique customer identifier |
|... | | |
| `name` | optional | optional |
| `surname` | optional | optional |
| ... | optional | optional |

##### MonthlyFeatures
| Attribute    | Type | Description |
|-------------|------|-------------|
| `customer_id` | String `FK` | Unique customer identifier |
| `total_transactions`       | Integer | Total transactions in the last 30 days |
| `total_amount`             | Decimal | Total transaction amount in the last 30 days |
| `average_transaction_amount` | Decimal | Mean amount per transaction over 30 days |

##### DailyAggregate
| Attribute          | Type    | Description |
|------------------|---------|-------------|
| `date`            | Date `PK`   | Transaction day (YYYY-MM-DD) |
| `customer_id` | String `FK` | Unique customer identifier |
| `total_amount`    | Decimal | Sum of transaction amounts for the day |
| `transaction_count` | Integer | Number of transactions for the day |


#### Item example:
```json
{"C000": 
    {"daily_sums": {
        "2026-01-19": {"amount": 380.65, "count": 2}, 
        "2026-01-26": {"amount": 341.64, "count": 2},
        ...
        "2026-01-27": {"amount": 176.41, "count": 1}
    }, 
    "features": {
        "total_txn_30d": 16, 
        "total_amount_30d": 3065.21,
        "avg_amount_30d": 191.58
    }
}
```

#### Table: `seen_events`

This table ensure the idempotency. With a small memory and reading cost, the system is able to understand if a certain event have been processed already, and avoids duplicates.

##### 
| Attribute    | Type | Description |
|-------------|------|-------------|
| `event_id` | String `PK` | The id of an event that have been already processed |
---

### Concurrent updates

* In our toy system, there are not concurrent updates to manage, considering that we have one single program, with one single process.
* In a real DynamoDB setup, concurrency would be handled using Optimistic locking. Optimistic locking uses a version number on each item to ensure updates only succeed if the item hasn’t been modified by others, preventing accidental overwrites and requiring a retry if a version mismatch occurs.
* This prevents lost updates when multiple consumers process the same customer concurrently.

---

## 4. Model versioning & deployment

### Model artifact storage

* Trained models are saved as serialized artifacts (e.g. `artifacts/high_value_customer_model.json`).
* Everytime we train a new model, the old one is versioned usign the timestamp of the training execution so it can be used in the future again.

---

### Rollout & rollback

* New models are deployed by:

  * updating the model artifact reference using the function `train()`
  * binding a folder at container running time using the command:
  `docker run --mount type=bind,src=./artifacts,dst=/artifacts streaming-score`, in this way we bind a host local folder to the container one in order to update the model whenever we want
  * restarting the scoring service/container
* Rollback is achieved by switching back to a previous model artifact

---

## 5. Replay / backfill

### Rebuilding features from event history

* Theoretically the **event log is the source of truth**.
* The system is able to recompute from scratch all the features in a consistent way.
* Since the system is simulated using a random genarator, it doesn't acutally read event from a log, but it can produce the same input setting a seed in the `simulated_kafka_stream()` function.

> **Note**
> The choice of random generated input was done in order to further train easily the model. In this way I didn't have to write manually the data, but generating it automatically, with my parameters of choice. The seed helped me to understand if the final features where calculated in the same way (setting up a seed forces the generator to produce always the same input, random is actually pseudorandom, but this we all know :P)

This supports:

* disaster recovery
* feature logic changes
* model retraining with historical data

---

### Large-scale backfills

For large-scale backfills in production, the system would:

* Read historical events from durable storage (Kafka with long retention)
* Process events in parallel using:

  * batch jobs (e.g. Spark / Flink)
  * or partitioned consumers
* Write recomputed state back to DynamoDB using throttled, idempotent writes
* Optionally backfill in time slices to limit blast radius
