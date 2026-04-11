import re


class LangGraphWorkflow:
    SQL_KEYWORDS = [
        "sales", "sold", "quantity", "turnover", "revenue", "product", "produit", "quantité", "chiffre", "total", "montant",
        "month", "month", "janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre",
        "vente", "vendu", "volume", "stock", "marge"
    ]
    VECTOR_KEYWORDS = [
        "why", "pourquoi", "explain", "explication", "trend", "tendance", "cause", "raison", "reasons", "analysis", "analyse",
        "behavior", "comportement", "profile", "anomaly", "anomalie", "recommend", "recommandation"
    ]

    def decide(self, question: str) -> dict:
        text = (question or "").lower()
        scores = {
            "sql": sum(1 for token in self.SQL_KEYWORDS if token in text),
            "vector": sum(1 for token in self.VECTOR_KEYWORDS if token in text),
        }

        if scores["vector"] >= 2 and scores["sql"] >= 1:
            mode = "hybrid"
        elif scores["sql"] >= 1:
            mode = "sql"
        elif scores["vector"] >= 1:
            mode = "vector"
        else:
            mode = "sql"

        return {
            "mode": mode,
            "reason": f"sql_score={scores['sql']} vector_score={scores['vector']}",
        }

    def should_use_sql(self, question: str) -> bool:
        return self.decide(question)["mode"] in ("sql", "hybrid")

    def should_use_vector(self, question: str) -> bool:
        return self.decide(question)["mode"] in ("vector", "hybrid")
