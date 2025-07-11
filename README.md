# 🔤 toCommon: Reducing Vocabulary Complexity in Language Models

Welcome to **toCommon**, an experimental and ambitious project aimed at reducing vocabulary complexity and improving the efficiency of large language models (LLMs). Though built upon a series of theoretical hypotheses, this repository lays the foundation for a potential startup venture pushing the boundaries of LLM design, training, and inference.

**"Now [Model](/DenseGroupAttention/models.py) for DenseGroupAttention is ready to train on, at same time, the edit system for Simple-Word Compound Substitution is in develope"**

## Core Idea

The central methodology is described in [**common\_words.pdf**](./common_words.pdf), titled:

**"Reducing Vocabulary Complexity in Large Language Models via Simple-Word Compound Substitution"**

This approach proposes a structured mechanism to simplify token vocabularies by intelligently replacing compound words and multi-word expressions with concatenated sequences of simpler, high-frequency base words. The goal is to reduce token redundancy and vocabulary bloat while preserving or improving semantic representation.

---

## Supporting Hypotheses and Techniques

In addition to vocabulary simplification, **toCommon** incorporates several complementary research ideas to enhance model efficiency and specialization:

### 1. Efficient Scaling and Specialization

**File:** [`Efficient_Scaling.pdf`](./Efficient_Scaling.pdf)
**Title:** *"Efficient Scaling and Specialization of Language Models via Layer Reuse and Domain-Focused Training"*

* Promotes transformer layer reuse across domains to reduce training costs.
* Enables domain-specific fine-tuning without degrading general performance.

### 2. Intentional Overfitting & Caching

**File:** [`Overfitting_and_Caching.pdf`](./Overfitting_and_Caching.pdf)
**Title:** *"Reducing AI Model Uncertainty via Intentional Overfitting and Structured Caching"*

* Advocates controlled overfitting on selected data to improve reliability.
* Introduces caching mechanisms to aid memory-like retrieval and reduce hallucinations.

### 3. Dataset Quality and Noise Filtering

**File:** [`Improved_Datasets.pdf`](./Improved_Datasets.pdf)
**Title:** *"Towards Improved Datasets in Machine Learning: Hypotheses on Pollution, Poison, and the Role of Misspellings"*

* Focuses on identifying and correcting dataset-level flaws.
* Explores the impact of "poison" examples and systematic misspellings on model quality.

### 4. 🔗 Local Semantic Grouping via Word Compounding

**Primary File:** [`Word_Compound.pdf`](./Word_Compound.pdf)
**Alt. Option:** [`Dense_Group_Attention.pdf`](./Dense_Group_Attention.pdf)
**Titles:**

* *"Word Compounding Layers: A Hypothesis on Efficient Local Semantic Grouping for Language Models"*
* *"Dense Group Attention: A Hypothesis on Local Contextual Embedding through Structured Word Concatenation"*

These works explore new attention and embedding structures to preserve local context, improve phrase-level semantics, and boost representational compactness in simplified-vocabulary models.

---

## Vision

While **toCommon** is primarily hypothesis-driven, we are confident in the direction and believe it can evolve into a **startup-grade system** offering:

* Smaller, faster, and more interpretable language models
* More efficient tokenization pipelines
* Improved accuracy on domain-specific tasks
* Lower computational and memory footprints for deployment

---

## 📂 Repository Structure

```
/
├── common_words.pdf
├── Efficient_Scaling.pdf
├── Overfitting_and_Caching.pdf
├── Improved_Datasets.pdf
├── Word_Compound.pdf
├── Dense_Group_Attention.pdf
├── DenseGroupAttention/                     # Model for DenseGroupAttention
│   └── models.py
├── LICENSE-CODE
└── README.md
```

---

## Project Status

**toCommon** is currently in **early-stage experimental development**.
We are actively building out the tokenizer, training pipeline, and evaluation framework based on the theories described.

> ⚠️ Note: The included concepts are largely theoretical and under validation, but early results and intuition suggest strong potential.

---

## 💬 Get Involved

If you're a researcher, engineer, or developer interested in vocabulary compression, efficient LLM architecture, or novel training paradigms, we welcome your contributions and feedback!

Open an issue, start a discussion, or contact us directly via \[email].

---

## 📄 License

This code repository is licensed under the MIT License. The use of DeepSeek-V2 models is subject to the DeepSeek-V2 Model License.

---
