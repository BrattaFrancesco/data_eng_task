import json
import numpy as np
import xgboost as xgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score


def build_training_dataset(data):
    X = []
    y = []

    threshold = 5000.0  # Threshold for high value customer

    for _, customer_data in data.items():
        features = customer_data.get("features", {})
        if not features:
            continue

        total_amount_30d = features["total_amount_30d"]

        # Feature vector
        X.append([
            features["total_txn_30d"],
            features["total_amount_30d"],
            features["avg_amount_30d"],
        ])

        # Synthetic label:
        # Custumer is high value (1) if total amount in last 30 days > $3000
        label = 1 if total_amount_30d > threshold else 0
        y.append(label)

    return np.array(X), np.array(y)

def train_model(X, y):
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.3, random_state=42
    )

    if len(np.unique(y_train)) < 2:
        print("Training split has only one class:", np.unique(y_train))
        return None

    model = xgb.XGBClassifier(
        objective="binary:logistic",
        max_depth=3,
        n_estimators=10,
        learning_rate=0.1,
        eval_metric="auc",
        random_state=42,
    )

    model.fit(X_train, y_train)

    preds = model.predict_proba(X_test)[:, 1]
    auc = roc_auc_score(y_test, preds)

    print(f"AUC: {auc:.3f}")

    model.save_model("artifacts/high_value_customer_model.json")

    return model

def score_customer(model, features):
    """
    Score a single customer using latest feature state.
    """
    feature_vector = np.array([[
        features["total_txn_30d"],
        features["total_amount_30d"],
        features["avg_amount_30d"],
    ]])

    score = model.predict_proba(feature_vector)[0][1]
    return score

def main():
    with open("src/data/balances.json", "r") as f:
        balances = json.load(f)
    
    X, y = build_training_dataset(balances)
    model = train_model(X, y)

if __name__ == "__main__":
    main()