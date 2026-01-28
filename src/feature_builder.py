import random
import uuid
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Generator

# Configure logging to file and console
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('feature_builder.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

WINDOW_30D = timedelta(days=30)
GRACE_PERIOD = timedelta(days=5)

#This function is the simulated producer.
#Simulates a Kafka topic emitting transaction events. 
#Includes duplicates and out-of-order delivery generation.
def simulated_kafka_stream(
    num_customers: int = 5,
    events_per_customer: int = 10,
    duplicate_rate: float = 0.1,
    out_of_order_rate: float = 0.3,
    seed=None,
) -> Generator[Dict, None, None]:

    if seed is not None:
        random.seed(seed) 

    base_time = datetime.now(tz=timezone.utc)
    events: List[Dict] = []

    for c in range(num_customers):
        customer_id = f"C{c:03d}"

        for i in range(events_per_customer):
            event_time = base_time - timedelta(days=random.randint(0, 40))

            event = {
                "event_id": str(uuid.uuid4()),
                "customer_id": customer_id,
                "event_time": event_time.isoformat(),
                "event_type": "transaction",
                "amount": round(random.uniform(10, 300), 2),
            }

            events.append(event)

            # introduce duplicates
            if random.random() < duplicate_rate:
                events.append(event.copy())

    # shuffle to simulate out-of-order events
    if random.random() < out_of_order_rate:
        random.shuffle(events)

    for event in events:
        yield event

#This class simulates a DynamoDB-like state store.
#Replace the in-memory structures with actual DynamoDB calls in production using the boto3 library.
#Set up DynamoDB table with partition key as customer_id (?)
class StateStore:

    def __init__(self):
        # = boto3.client('dynamodb')
        self.balances: Dict[str, Dict] = {} # to track customer balances
        self.seen_event_ids = set() # to track duplicates easily

    # Works on the idempotency table
    def is_duplicate(self, event_id: str) -> bool:
        return event_id in self.seen_event_ids

    # Works on the idempotency table
    def mark_seen(self, event_id: str):
        self.seen_event_ids.add(event_id)

    # Works on the customer balances table
    def get_customer(self, customer_id: str) -> Dict:
        if customer_id not in self.balances:
            self.balances[customer_id] = {
                "daily_sums": {}  # {date: {"amount": float, "count": int}}
            }
        return self.balances[customer_id]
    
    def update_daily_sum(self, customer_id: str, event: Dict):
        customer = self.get_customer(customer_id)
        
        event_time = datetime.fromisoformat(event["event_time"])
        date_key = event_time.date().isoformat()  # "2026-01-28"
        
        # If date exists, add to it; otherwise create new entry
        if date_key not in customer["daily_sums"]:
            customer["daily_sums"][date_key] = {"amount": 0.0, "count": 0}

        customer["daily_sums"][date_key]["amount"] += event["amount"]
        customer["daily_sums"][date_key]["count"] += 1
    
    def evict_old_events(self, customer_id: str, cutoff: datetime):
        customer = self.get_customer(customer_id)
        keys_to_delete = [
            date_key for date_key in customer["daily_sums"]
            if datetime.fromisoformat(date_key).date() < cutoff.date()
        ]
        for key in keys_to_delete:
            del customer["daily_sums"][key]

#consumer class that processes events and computes features
class FeatureBuilder:
    def __init__(self, state_store: StateStore):
        self.state_store = state_store
    
    def _evict_old_events(self, customer_id, reference_time: datetime):
        cutoff = reference_time - WINDOW_30D - GRACE_PERIOD
        self.state_store.evict_old_events(customer_id,cutoff)

    def _compute_features(self, customer_id) -> Dict:
        customer = self.state_store.get_customer(customer_id)
        events = customer["daily_sums"]

        print(f"  Daily sums: {list(events.keys())}")
        print(f"  Counts: {[(k, v['count']) for k, v in events.items()]}")
    

        if not events:
            return {
                "total_txn_30d": 0,
                "total_amount_30d": 0.0,
                "avg_amount_30d": 0.0,
            }

        total_txn = sum(event["count"] for event in events.values())
        total_amount = sum(event["amount"] for event in events.values())

        return {
            "total_txn_30d": total_txn,
            "total_amount_30d": round(total_amount, 2),
            "avg_amount_30d": round(total_amount / total_txn, 2),
        }

    def process_event(self, event: Dict):
        # basic validation
        required_fields = {"event_id", "customer_id", "event_time", "event_type", "amount"}
        if not required_fields.issubset(event):
            missing = required_fields - set(event.keys())
            logger.warning(f"Skipping event - missing fields: {missing}. Event: {event}")
            return

        event_id = event["event_id"]
        if self.state_store.is_duplicate(event_id):
            logger.warning(f"Skipping duplicate event: {event_id}")
            return

        self.state_store.mark_seen(event_id)

        customer_id = event["customer_id"]
        # Check if event_time is valid
        try:
            event_time = datetime.fromisoformat(event["event_time"].replace("Z", "+00:00"))
        except (ValueError, AttributeError) as e:
            logger.error(f"Skipping event {event_id} - invalid event_time: {event['event_time']}. Error: {e}")
            return

        # Check if amount is numeric
        try:
            amount = float(event["amount"])
            if amount < 0:
                logger.error(f"Skipping event {event_id} - negative amount: {amount}")
                return
        except (ValueError, TypeError) as e:
            logger.error(f"Skipping event {event_id} - invalid amount: {event['amount']}. Error: {e}")
            return
        self.state_store.update_daily_sum(customer_id, event)

        if not hasattr(self, 'max_event_time'):
            self.max_event_time = event_time
        else:
            self.max_event_time = max(self.max_event_time, event_time)

        self._evict_old_events(customer_id, self.max_event_time)
        return self._compute_features(customer_id)

# This function simulates batching by 5-minute time windows (tumbling windows).
def batch_by_time_window(kafka_stream, window_size=timedelta(minutes=5)):
    """
    Groups events by 5-minute time buckets (based on event_time, not arrival time)
    """
    batches = {}  # {window_key: {customer_id: [events]}}
    
    for event in kafka_stream:
        event_time = datetime.fromisoformat(event["event_time"])
        
        # Include year/month/day/hour/minute to ensure events from different dates don't collide
        window_key = (
            event_time.year,
            event_time.month,
            event_time.day,
            event_time.hour,
            event_time.minute // 5 * 5
        )
        
        if window_key not in batches:
            batches[window_key] = {}
        
        customer_id = event["customer_id"]
        if customer_id not in batches[window_key]:
            batches[window_key][customer_id] = []
        
        batches[window_key][customer_id].append(event)
    
    return batches.values()

def main():
    state_store = StateStore()
    feature_builder = FeatureBuilder(state_store)

    for customer_batches in batch_by_time_window(simulated_kafka_stream(num_customers=1)): ## Use seed to show replyability
        for _, events in customer_batches.items():
            for event in events:
                print("Processing event:", event["event_id"], "time:", 
                      datetime.fromisoformat(event["event_time"]), 
                      "amount:", event["amount"])
                print(f"Feature update: {feature_builder.process_event(event)}\n")

if __name__ == "__main__":
    main()