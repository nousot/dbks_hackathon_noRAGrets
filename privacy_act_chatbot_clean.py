# Databricks notebook source
# MAGIC %pip install transformers==4.30.2 "unstructured[pdf,docx]==0.10.30" llama-index==0.9.40 databricks-vectorsearch==0.20 pydantic==1.10.9 mlflow==2.9.0 protobuf==3.20.0 openai==1.10.0 langchain-openai langchain torch torchvision torchaudio FlagEmbedding
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import io
import re

# DBTITLE 1,Helper function for read_from_text
from unstructured.partition.auto import partition


def extract_doc_text(x : bytes) -> str:
  # Read files and extract the values with unstructured
  sections = partition(file=io.BytesIO(x))
  print(sections)
  def clean_section(txt):
    txt = re.sub(r'\n', '', txt)
    return re.sub(r' ?\.', '.', txt)
  # Default split is by section of document, concatenate them all together because we want to split by sentence instead.
  return "\n".join([clean_section(s.text) for s in sections]) 

# COMMAND ----------

# DBTITLE 1,Databricks BGE Embedding
import pandas as pd
from pyspark.sql.functions import pandas_udf

# COMMAND ----------


@pandas_udf("array<float>")
def get_embedding(contents: pd.Series) -> pd.Series:
    import mlflow.deployments
    deploy_client = mlflow.deployments.get_deploy_client("databricks")
    def get_embeddings(batch):
        #Note: this will fail if an exception is thrown during embedding creation (add try/except if needed) 
        response = deploy_client.predict(endpoint="databricks-bge-large-en", inputs={"input": batch})
        return [e['embedding'] for e in response.data]

    # Splitting the contents into batches of 150 items each, since the embedding model takes at most 150 inputs per request.
    max_batch_size = 150
    batches = [contents.iloc[i:i + max_batch_size] for i in range(0, len(contents), max_batch_size)]

    # Process each batch and collect the results
    all_embeddings = []
    for batch in batches:
        all_embeddings += get_embeddings(batch.tolist())

    return pd.Series(all_embeddings)

# COMMAND ----------

import logging
import os
from typing import Iterator

import mypy_extensions
import pandas as pd
from langchain_openai import AzureOpenAIEmbeddings
from llama_index import Document, set_global_tokenizer
# DBTITLE 1,OpenAI Client and read as chunk function defined
from llama_index.langchain_helpers.text_splitter import SentenceSplitter
from llama_index.node_parser import SemanticSplitterNodeParser
from openai import AzureOpenAI
from pyspark.sql import functions as F
from pyspark.sql.functions import pandas_udf
from transformers import AutoTokenizer

os.environ["AZURE_OPENAI_API_KEY"] = dbutils.secrets.get(scope='dev_demo', key='azure_openai_api_key')
os.environ["AZURE_OPENAI_ENDPOINT"] = "https://nous-ue2-openai-sbx-openai.openai.azure.com/"

embeddings = AzureOpenAIEmbeddings(
    azure_deployment="nous-ue2-openai-sbx-base-deploy-text-embedding-ada-002",
    openai_api_version="2023-05-15",
)

client = AzureOpenAI(
    api_key = dbutils.secrets.get(scope='dev_demo', key='azure_openai_api_key'),
    api_version = "2023-05-15",
    azure_endpoint = "https://nous-ue2-openai-sbx-openai.openai.azure.com/",
    )

# Reduce the arrow batch size as our PDF can be big in memory
spark.conf.set("spark.sql.execution.arrow.maxRecordsPerBatch", 10)

os.environ["HF_HOME"] = '/tmp'

@pandas_udf("array<string>")
def read_as_chunk(batch_iter: Iterator[pd.Series]) -> Iterator[pd.Series]:
    #set embedding model
    # embed_model = "nous-ue2-openai-sbx-base-deploy-text-embedding-ada-002"
    #set llama2 as tokenizer to match our model size (will stay below BGE 1024 limit)
    set_global_tokenizer(
      AutoTokenizer.from_pretrained("hf-internal-testing/llama-tokenizer", cache_dir = '/tmp')
    )
    # splitter = SemanticSplitterNodeParser(
    # buffer_size=1, breakpoint_percentile_threshold=95, embed_model=embeddings
    # )
    #Sentence splitter from llama_index to split on sentences
    base_splitter = SentenceSplitter(chunk_size=500, chunk_overlap=25)
    def extract_and_split(b):
      txt = extract_doc_text(b)
      nodes = base_splitter.get_nodes_from_documents([Document(text=txt)])
      logging.info(f"from chunk function: {txt}")
      
      return [n.text for n in nodes]

    for x in batch_iter:
        yield x.apply(extract_and_split)

# COMMAND ----------

# DBTITLE 1,No need to run this (table already created)
# %sql
# --Note that we need to enable Change Data Feed on the table to create the index
# CREATE TABLE IF NOT EXISTS demo.hackathon.databricks_pdf_documentation_openai (
#   id BIGINT GENERATED BY DEFAULT AS IDENTITY,
#   url STRING,
#   content STRING,
#   embedding ARRAY <FLOAT>
# ) TBLPROPERTIES (delta.enableChangeDataFeed = true); 

# COMMAND ----------

# DBTITLE 1,Get Embeddings function defined
def open_ai_embeddings(contents):
    embed_model = "nous-ue2-openai-sbx-base-deploy-text-embedding-ada-002"

    response = client.embeddings.create(
        input = contents,
        model = embed_model
    )

    return response.data[0].embedding

# COMMAND ----------

import os

import mypy_extensions
import pandas as pd
# DBTITLE 1,BGE Vector Search Client
from databricks.vector_search.client import VectorSearchClient
from pyspark.sql import functions as F

# COMMAND ----------

# DBTITLE 1,Write to databricks_pdf_documentation_openai
# from pyspark.sql import functions as F
# import mypy_extensions
# import pandas as pd
# import os

# # # Reduce the arrow batch size as our PDF can be big in memory
# # spark.conf.set("spark.sql.execution.arrow.maxRecordsPerBatch", 10)

# # os.environ["HF_HOME"] = '/tmp'

# volume_folder = f"/Volumes/demo/hackathon/privacy_act_docs/*"

# # ADA Embeddings
# temp = (spark.table('demo.hackathon.pdf_raw')
#         .withColumn("content", F.explode(read_as_chunk("content")))
#         .withColumn("ada_embedding", F.lit(open_ai_embeddings("content")))
#         .withColumn("id", F.monotonically_increasing_id())
#         .withColumn("state", F.split(F.col("path"), "/")[5])
#         .selectExpr('id', 'path as url', 'content', 'ada_embedding', 'state')
#         )

# (temp.write
#     .option("checkpointLocation", f'dbfs:{volume_folder}/checkpoints/pdf_chunk_openai')
#     .option("overwriteSchema", "true")
#     .mode("overwrite")
#     .saveAsTable('demo.hackathon.databricks_pdf_documentation_openai'))

# # BGE Embeddings
# temp = (spark.table('demo.hackathon.pdf_raw')
#         .withColumn("content", F.explode(read_as_chunk("content")))
#         .withColumn("bge_embedding", F.lit(get_embedding("content")))
#         .withColumn("id", F.monotonically_increasing_id())
#         .withColumn("state", F.split(F.col("path"), "/")[5])
#         .selectExpr('id', 'path as url', 'content', 'bge_embedding', 'state')
#         )

# (temp.write
#     .option("checkpointLocation", f'dbfs:{volume_folder}/checkpoints/pdf_chunk_baai')
#     .option("overwriteSchema", "true")
#     .mode("overwrite")
#     .saveAsTable('demo.hackathon.databricks_pdf_documentation_baai'))

# COMMAND ----------

# %sql
# use catalog `demo`; 
# select count(*), count(distinct content) from `hackathon`.`databricks_pdf_documentation_baai` 



# COMMAND ----------

# table = spark.table('demo.hackathon.databricks_pdf_documentation_baai')
# table = table.dropDuplicates(subset=["content"])

# (table.write
#     .option("overwriteSchema", "true")
#     .mode("overwrite")
#     .saveAsTable('demo.hackathon.databricks_pdf_documentation_baai'))

# COMMAND ----------

# %sql
# use catalog `demo`; 
# select count(*), count(distinct content) from `hackathon`.`databricks_pdf_documentation_baai` 

# COMMAND ----------

# %sql
# use catalog `demo`; 
# select count(*), count(distinct content) from `hackathon`.`databricks_pdf_documentation_openai` 

# COMMAND ----------

# table = spark.table('demo.hackathon.databricks_pdf_documentation_openai')
# table = table.dropDuplicates(subset=["content"])

# (table.write
#     .option("overwriteSchema", "true")
#     .mode("overwrite")
#     .saveAsTable('demo.hackathon.databricks_pdf_documentation_openai'))

# COMMAND ----------

# %sql
# use catalog `demo`; 
# select count(*), count(distinct content) from `hackathon`.`databricks_pdf_documentation_openai` 

# COMMAND ----------

# # workaround for not having ML cluster
# temp = (spark.table('demo.hackathon.databricks_pdf_documentation_openai')
#         .withColumn("state", F.split(F.col("url"), "/")[5])
#         .selectExpr('id', 'url', 'content', 'embedding', 'state')
#         )

# (temp.write
#     .option("checkpointLocation", f'dbfs:{volume_folder}/checkpoints/pdf_chunk_openai')
#     .option("overwriteSchema", "true")
#     .mode("overwrite")
#     .saveAsTable('demo.hackathon.databricks_pdf_documentation_openai'))

# COMMAND ----------

vsc_bge = VectorSearchClient(disable_notice=True)
vs_index_fullname_bge = "demo.hackathon.bge_self_managed_index"
endpoint_name_bge = "bge_vector_search"

# COMMAND ----------

# %sql
# ALTER TABLE demo.hackathon.databricks_pdf_documentation_baai SET TBLPROPERTIES (delta.enableChangeDataFeed = true)

# COMMAND ----------

# DBTITLE 1,Endpoint creation (one-time run)
# #vsc_bge.create_endpoint(name=endpoint_name_bge, endpoint_type="STANDARD")
# vsc_bge.create_delta_sync_index(
#     endpoint_name=endpoint_name_bge,
#     index_name=vs_index_fullname_bge,
#     source_table_name="demo.hackathon.databricks_pdf_documentation_baai",
#     pipeline_type="TRIGGERED", #Sync needs to be manually triggered
#     primary_key="id",
#     embedding_dimension=1024, #Match your model embedding size (bge = 1024, ada = 1536)
#     embedding_vector_column="bge_embedding"
#   )

# COMMAND ----------

# DBTITLE 1,ADA Vector Search Client
from databricks.vector_search.client import VectorSearchClient

vsc_ada = VectorSearchClient(disable_notice=True)
vs_index_fullname_ada = "demo.hackathon.ada_self_managed_index"
endpoint_name_ada = "ada_vector_search"

# COMMAND ----------

# # vsc_ada.create_endpoint(name=endpoint_name_ada, endpoint_type="STANDARD")
# vsc_ada.create_delta_sync_index(
#     endpoint_name=endpoint_name_ada,
#     index_name=vs_index_fullname_ada,
#     source_table_name="demo.hackathon.databricks_pdf_documentation_openai",
#     pipeline_type="TRIGGERED", #Sync needs to be manually triggered
#     primary_key="id",
#     embedding_dimension=1536, #Match your model embedding size (bge = 1024, ada = 1536)
#     embedding_vector_column="ada_embedding"
#   )

# COMMAND ----------

# DBTITLE 1,Resync BGE Embeddings
# # Resync our index with new data
# vsc_bge.get_index(endpoint_name_bge, vs_index_fullname_bge).sync()

# COMMAND ----------

# DBTITLE 1,Resync ADA Embeddings
# # Resync our index with new data
# vsc_ada.get_index(endpoint_name_ada, vs_index_fullname_ada).sync()

# COMMAND ----------

import ast

import mlflow.deployments


def get_state_from_query(query):
    client = mlflow.deployments.get_deploy_client("databricks")
    inputs = {
        "messages": [
            {
                "role": "user",
                "content": f"""
                You determine if there are any US states present in this text: {query}.
                Your response should be JSON like the following:
                {{ 
                    "state": []
                }}

                """
            }
        ],
        "max_tokens": 64,
        "temperature": 0
    }

    response = client.predict(endpoint="databricks-mixtral-8x7b-instruct", inputs=inputs)
    return response["choices"][0]['message']['content']

# COMMAND ----------

# DBTITLE 1,Test prompts (call embedding endpoint here)

# from mlflow.deployments import get_deploy_client
from pprint import pprint

# bge-large-en Foundation models are available using the /serving-endpoints/databricks-bge-large-en/invocations api. 
# deploy_client = get_deploy_client("databricks")

query = f"What rights can consumers exercise?"
# What is considered biometric data?
response = get_state_from_query(query)
cleaned_response = response.replace("```json", "")
cleaned_response = cleaned_response.replace("```", "")
filters = ast.literal_eval(cleaned_response)

# COMMAND ----------

# ADA embedding search
if filters["state"] != []:
  results_ada = vsc_ada.get_index(endpoint_name_ada, vs_index_fullname_ada).similarity_search(
    query_vector = open_ai_embeddings(query),
    columns=["id","state", "url", "content"],
    filters=filters,
    num_results=10)
  docs_ada = results_ada.get('result', {}).get('data_array', [])
  pprint(docs_ada)
else:
  results_ada = vsc_ada.get_index(endpoint_name_ada, vs_index_fullname_ada).similarity_search(
    query_vector = open_ai_embeddings(query),
    columns=["id","state", "url", "content"],
    num_results=10)
  docs_ada = results_ada.get('result', {}).get('data_array', [])
  pprint(docs_ada)

# COMMAND ----------

# Ad-hoc BGE embedding function
import mlflow.deployments

bge_deploy_client = mlflow.deployments.get_deploy_client("databricks")

def get_bge_embeddings(query):
    #Note: this will fail if an exception is thrown during embedding creation (add try/except if needed) 
    response = bge_deploy_client.predict(endpoint="databricks-bge-large-en", inputs={"input": query})
    #return [e['embedding'] for e in response.data]
    return response.data[0]['embedding']

# COMMAND ----------

# BGE embedding search
if filters["state"] != []:
  results_bge = vsc_bge.get_index(endpoint_name_bge, vs_index_fullname_bge).similarity_search(
    query_vector = get_bge_embeddings(query),
    columns=["id","state", "url", "content"],
    filters=filters,
    num_results=10)
  docs_bge = results_bge.get('result', {}).get('data_array', [])
  pprint(docs_bge)
else:
  results_bge = vsc_bge.get_index(endpoint_name_bge, vs_index_fullname_bge).similarity_search(
    query_vector = get_bge_embeddings(query),
    columns=["id","state", "url", "content"],
    num_results=10)
  docs_bge = results_bge.get('result', {}).get('data_array', [])
  pprint(docs_bge)

# COMMAND ----------

# This filter is *working* hard-coded, but the RAG is still coming back with incorrect results.

# from mlflow.deployments import get_deploy_client
# from pprint import pprint
# # bge-large-en Foundation models are available using the /serving-endpoints/databricks-bge-large-en/invocations api. 
# deploy_client = get_deploy_client("databricks")
# query = f"When does the Colorado Privacy Act take effect?"
# #content
# results = vsc.get_index(endpoint_name, vs_index_fullname).similarity_search(
#   query_vector=open_ai_embeddings(query),
#   columns=["state", "url", "content"],
#   filters={"state": "Colorado"},
#   num_results=10)
# docs = results.get('result', {}).get('data_array', [])
# pprint(docs)


# COMMAND ----------

docs = docs_bge + docs_ada
dedup_docs = list(set(tuple(i) for i in docs))
final_list = [list(i) for i in dedup_docs]

print(final_list)
# print(len(docs_bge), len(docs_ada) , len(dedup_docs))


# COMMAND ----------

from FlagEmbedding import FlagReranker
# DBTITLE 1,Reranking with bge-reranker-large
# Load model directly
from transformers import AutoModelForSequenceClassification, AutoTokenizer

tokenizer = AutoTokenizer.from_pretrained("BAAI/bge-reranker-large")
model = AutoModelForSequenceClassification.from_pretrained("BAAI/bge-reranker-large")

reranker = FlagReranker('BAAI/bge-reranker-large', use_fp16=True) # Setting use_fp16 to True speeds up computation with a slight performance degradation

query_and_docs = [[query, d[1]] for d in final_list]

scores = reranker.compute_score(query_and_docs)

reranked_docs = sorted(list(zip(final_list, scores)), key=lambda x: x[1], reverse=True)

pprint(reranked_docs)

# COMMAND ----------

#reranked_docs[0][0][3].replace("\n"," ")

# COMMAND ----------

userquery = '''Summarize this result: '''

def mixtral_query(userquery):
    client = mlflow.deployments.get_deploy_client("databricks")
    inputs = {
        "messages": [{"role":"user","content":f"{userquery} {reranked_docs[0][0][3]}"}],
        "max_tokens": 1500,
        "temperature": 0.8
    }

    response = client.predict(endpoint="databricks-mixtral-8x7b-instruct", inputs=inputs)
    return response["choices"][0]['message']['content']

# COMMAND ----------

print(mixtral_query(userquery))

# COMMAND ----------

# print LLM output
print(mixtral_query(userquery), 
f"\n\nDocument from State: {reranked_docs[0][0][1]}",
f"\nResult id: {reranked_docs[0][0][0]}",
f"\nDocument path: {reranked_docs[0][0][2]}"
)
