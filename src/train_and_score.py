import json
import numpy as np
import xgboost as xgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score


def build_training_dataset(data):
    X = []
    y = []

    threshold = 2600.0  # Threshold for high value customer

    for _, customer_data in data.items():
        features = customer_data.get("features", {})
        if not features:
            continue

        total_amount_30d = features["total_amount_30d"]

        # Feature vector
        X.append([
            features["total_txn_30d"],
            features["avg_amount_30d"],
        ])

        # Synthetic label:
        # Custumer is high value (1) if total amount in last 30 days > threshold
        label = 1 if total_amount_30d > threshold else 0
        
        p_noise = 0.1  # 10% labels flipped randomly
        # Flip label with probability
        if np.random.rand() < p_noise:
            label = 1 - label
        
        y.append(label)

    return np.array(X), np.array(y)

def train_model(X, y):
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.3, random_state=42
    )
    X_train,X_val, y_train, y_val = train_test_split(X_train,y_train,train_size = 0.8 )

    if len(np.unique(y_train)) < 2:
        print("Training split has only one class:", np.unique(y_train))
        return None

    model = xgb.XGBClassifier(
        objective="binary:logistic",
        max_depth=3,
        n_estimators=100,
        learning_rate=0.1,
        eval_metric="auc",
        random_state=42,
        early_stopping_rounds=10,
    )

    model.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)]
    )

    preds = model.predict_proba(X_test)[:, 1]
    auc = roc_auc_score(y_test, preds)

    print(f"AUC: {auc:.3f}")

    model.save_model("artifacts/high_value_customer_model.json")

    return model

def score_customer(model_filename, features):

    model = xgb.XGBClassifier()
    model.load_model(model_filename)

    feature_vector = np.array([[
        features["total_txn_30d"],
        features["avg_amount_30d"],
    ]])

    score = model.predict_proba(feature_vector)[0][1]
    return score

def main():
    with open("src/data/balances.json", "r") as f:
        balances = json.load(f)
    
    X, y = build_training_dataset(balances)
    train_model(X, y)

#if __name__ == "__main__":
    #main()