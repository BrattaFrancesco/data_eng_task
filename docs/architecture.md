# Architecture

This document describes the architecture of the streaming feature pipeline and ML scoring system implemented in this repository. The system simulates a real-time data platform using Kafka-like event ingestion, DynamoDB-style state storage, and an XGBoost model for customer value prediction. The design emphasizes **replayability, idempotency, and reproducibility**.

---

## 1. Kafka topics & message structure

### Topic names

The system is conceptually organized around one Kafka topic:

* **`customer-events`**
  Raw transactional events emitted by upstream producers.

> In the current implementation, Kafka is simulated via a Python generator that create events randomly using certain parameters. Afterwards, there is a function that simulates a tumbling window of 5 minutes, grouping those events by time window and customer. In this way, the script is able to process events that are part of the same window (5 minutes long) by customer id, respecting the message key logic. It is a simulation of a window function.

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
* This ensures that events for the same customer are routed to the same partition

---

## 2. Ordering, duplicates, and delivery semantics

### Delivery guarantees

* The system assumes **at-least-once delivery** semantics. In this case we don't have networks that can cause loss of data, so the data is always processed at least once.
* But to simulate the consequences of loss of data, even though it doesn't actually happen, events may be:
  * duplicated
  * delivered out of order
* This reflects real-world Kafka behavior under retries or failures.

---

### Duplicate handling

* Each event contains a globally unique `event_id`.
* A **deduplication mechanism** tracks processed `event_id`, using an idempotent table in the in-memory DynamoDB (that in this case is a simple Python set recording all the events that occurred).
* Events with already-seen IDs are ignored.
* This makes event processing **idempotent**.

---

### Ordering strategy

* The window function (simulated) partitions by customer_id, which ensures that all events for the same customer are delivered to the same partition, giving us per-customer ordering. In our case we just process all customers with the same ID within a 5-minutes interval.
* Still, given the random shuffle I did in the simulated input, some events arrive late. Keeping track of the last event_time, the system is able to evict outdated amounts that are out of the 30 days window, using `evict_old_events()`.

---

## 3. State storage

### DynamoDB tables design

The system uses a DynamoDB-style key-value store (simulated in memory).

#### Table: `balances`

> **Note** 
> In the current implementation this informations are stored in one single table, as you can see in the item example. This is a design choice to simplify the development, since the architectural part was not covered in the assesment cretaria. In a real world scenario, the daily aggretates and the features should be included or not in the main table depending on the system requirements. It is obvious that this implementation is faster, since it doesn't require any join between tables, but is not scalable. In the schema I will show what conceptually makes more sense to me, but then as I said, in reality the implementation can change based on the system requirements.

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

We keep track of the last 30 days sums because we need to recalculate the features everytime the monthly period expires, that happens when we receive a new event time that goes further in time (finishing one 30 days period and starting a new one)

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

This table ensures the idempotency. With a small memory and reading cost, the system is able to understand if a certain event has been processed already, and avoids duplicates.

##### 
| Attribute    | Type | Description |
|-------------|------|-------------|
| `event_id` | String `PK` | The id of an event that have been already processed |
---

### Concurrent updates

* In our toy system, there are no concurrent updates to manage, considering that we have one single program, with one single process.
* In a real DynamoDB setup, concurrency would be handled using Optimistic locking. Optimistic locking uses a version number on each item to ensure updates only succeed if the item hasn’t been modified by others, preventing accidental overwrites and requiring a retry if a version mismatch occurs.
* This prevents lost updates when multiple consumers process the same customer concurrently.

---

## 4. Model versioning & deployment

### Model artifact storage

* The model scores high value customers, gives a score to customers predicting if they will be more prone to spend money in the future.
* Trained models are saved as serialized artifacts (e.g. `artifacts/high_value_customer_model.json`).
* Every time we train a new model, the old one is versioned using the timestamp of the training execution so it can be used in the future again.

Here you can see the hyperparameters used for the training, whith a brief explaination:
```
 model = xgb.XGBClassifier(
        objective="binary:logistic", #we need a binary classification here
        max_depth=3, # considering how simple the features are this is enough
        n_estimators=100, # 100 estimators but with an early stop criteria
        learning_rate=0.1,
        eval_metric="auc",
        random_state=42,
        early_stopping_rounds=10, # this allows us to use enough estimators until the accuracy on the validation set do not increase for at least 10 rounds
    )
 ```

 Due to the nature of the dataset itself, some artificial noise introduction was needed to avoid overfitting. Along with that, the early stopping and the removal of the feature on which I did the labelling also helped with avoiding overfitting (with an AUC that lays between 0.7 and 0.9, depending on the noise introduction).

 In fact, initially I was achieving an almost perfect AUC, very close to 1. I realised that this was caused by the random nature of the data, and decided first to increase the number of customers and their transactions (from 10 to 100 customers, and from 5 to 20 events). Then I introduced the noise and used the early stopping technique. 
 In this way the model is able to score the high value customers (prone to spend more) better.

 > **IMPORTANT**
 > To label data I used a fixed threshold. I chose it by trials, seeing which one gave me a better separation of labels. My main problem was that if chosen poorly, the labels were only of one type, not allowing the training. I could have tried different methods (average total spending of customers for example or an approach with percentiles), but I think in this case it wouldn't make sense, considering the nature of the data.

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
* Since the system is simulated using a random generator, it doesn't actually read events from a log, but it can produce the same input setting a seed in the `simulated_kafka_stream()` function.

> **Note**
> The choice of random generated input was done in order to further train easily the model. In this way I didn't have to write manually the data, but generating it automatically, with my parameters of choice. The seed helped me to understand if the final features were calculated in the same way (setting up a seed forces the generator to produce always the same input, random is actually pseudorandom, but this we all know :P)

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
