# Databricks notebook source
# MAGIC %pip install transformers==4.30.2 "unstructured[pdf,docx]==0.10.30" llama-index==0.9.40 databricks-vectorsearch==0.20 pydantic==1.10.9 mlflow==2.9.0 protobuf==3.20.0 openai==1.10.0 langchain-openai langchain torch torchvision torchaudio FlagEmbedding
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Function to clean PDF text
import io
import re

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

# DBTITLE 1,BGE embedding function
import pandas as pd
from pyspark.sql.functions import pandas_udf

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

# DBTITLE 1,Azure OpenAI configuration
import logging
import os

from langchain_openai import AzureOpenAIEmbeddings
from openai import AzureOpenAI

os.environ["AZURE_OPENAI_API_KEY"] = dbutils.secrets.get(scope='dev_demo', key='azure_openai_api_key')
os.environ["AZURE_OPENAI_ENDPOINT"] = dbutils.secrets.get(scope='dev_demo', key='azure_openai_endpoint')

embeddings = AzureOpenAIEmbeddings(
    azure_deployment="nous-ue2-openai-sbx-base-deploy-text-embedding-ada-002",
    openai_api_version="2023-05-15",
)

client = AzureOpenAI(
    api_key = os.environ["AZURE_OPENAI_API_KEY"],
    api_version = "2023-05-15",
    azure_endpoint = os.environ["AZURE_OPENAI_ENDPOINT"],
    )

# COMMAND ----------

# DBTITLE 1,Function to chunk the text
from pyspark.sql import functions as F
from transformers import AutoTokenizer
from typing import Iterator
import mypy_extensions

from llama_index import Document, set_global_tokenizer
from llama_index.langchain_helpers.text_splitter import SentenceSplitter
from llama_index.node_parser import SemanticSplitterNodeParser

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

# DBTITLE 1,Create table to store Ada embeddings
# MAGIC %sql
# MAGIC --Note that we need to enable Change Data Feed on the table to create the index
# MAGIC CREATE TABLE IF NOT EXISTS demo.hackathon.databricks_pdf_documentation_openai (
# MAGIC   id BIGINT GENERATED BY DEFAULT AS IDENTITY,
# MAGIC   url STRING,
# MAGIC   content STRING,
# MAGIC   embedding ARRAY <FLOAT>
# MAGIC ) TBLPROPERTIES (delta.enableChangeDataFeed = true); 

# COMMAND ----------

# DBTITLE 1,Ada embeddings function
def open_ai_embeddings(contents):
    embed_model = "nous-ue2-openai-sbx-base-deploy-text-embedding-ada-002"

    response = client.embeddings.create(
        input = contents,
        model = embed_model
    )

    return response.data[0].embedding

# COMMAND ----------

# DBTITLE 1,Get embeddings and write to Delta table
from pyspark.sql import functions as F
import mypy_extensions

# # Reduce the arrow batch size as our PDF can be big in memory
# spark.conf.set("spark.sql.execution.arrow.maxRecordsPerBatch", 10)

# os.environ["HF_HOME"] = '/tmp'

volume_folder = f"/Volumes/demo/hackathon/privacy_act_docs/*"

# ADA Embeddings
temp = (spark.table('demo.hackathon.pdf_raw')
        .withColumn("content", F.explode(read_as_chunk("content")))
        .withColumn("ada_embedding", F.lit(open_ai_embeddings("content")))
        .withColumn("id", F.monotonically_increasing_id())
        .withColumn("state", F.split(F.col("path"), "/")[5])
        .selectExpr('id', 'path as url', 'content', 'ada_embedding', 'state')
        )

(temp.write
    .option("checkpointLocation", f'dbfs:{volume_folder}/checkpoints/pdf_chunk_openai')
    .option("overwriteSchema", "true")
    .mode("overwrite")
    .saveAsTable('demo.hackathon.databricks_pdf_documentation_openai'))

# BGE Embeddings
temp = (spark.table('demo.hackathon.pdf_raw')
        .withColumn("content", F.explode(read_as_chunk("content")))
        .withColumn("bge_embedding", F.lit(get_embedding("content")))
        .withColumn("id", F.monotonically_increasing_id())
        .withColumn("state", F.split(F.col("path"), "/")[5])
        .selectExpr('id', 'path as url', 'content', 'bge_embedding', 'state')
        )

(temp.write
    .option("checkpointLocation", f'dbfs:{volume_folder}/checkpoints/pdf_chunk_baai')
    .option("overwriteSchema", "true")
    .mode("overwrite")
    .saveAsTable('demo.hackathon.databricks_pdf_documentation_baai'))

# COMMAND ----------

# DBTITLE 1,Review Ada embeddings table
# MAGIC %sql
# MAGIC use catalog `demo`; select * from `hackathon`.`databricks_pdf_documentation_openai` limit 1;

# COMMAND ----------

# DBTITLE 1,Review BGE embeddings table
# MAGIC %sql
# MAGIC use catalog `demo`; select * from `hackathon`.`databricks_pdf_documentation_baai` limit 1;

# COMMAND ----------

# DBTITLE 1,BGE Vector Search Client
from databricks.vector_search.client import VectorSearchClient

vsc_bge = VectorSearchClient(disable_notice=True)
vs_index_fullname_bge = "demo.hackathon.bge_self_managed_index"
endpoint_name_bge = "bge_vector_search"

# COMMAND ----------

# DBTITLE 1,ADA Vector Search Client
from databricks.vector_search.client import VectorSearchClient

vsc_ada = VectorSearchClient(disable_notice=True)
vs_index_fullname_ada = "demo.hackathon.ada_self_managed_index"
endpoint_name_ada = "ada_vector_search"

# COMMAND ----------

# %sql
# ALTER TABLE demo.hackathon.databricks_pdf_documentation_baai SET TBLPROPERTIES (delta.enableChangeDataFeed = true)

# COMMAND ----------

# DBTITLE 1,BGE Vector Search Endpoint - one time run
vsc_bge.create_endpoint(name=endpoint_name_bge, endpoint_type="STANDARD")

# COMMAND ----------

# DBTITLE 1,BGE Vector Search Index - one time run
vsc_bge.create_delta_sync_index(
    endpoint_name=endpoint_name_bge,
    index_name=vs_index_fullname_bge,
    source_table_name="demo.hackathon.databricks_pdf_documentation_baai",
    pipeline_type="TRIGGERED", #Sync needs to be manually triggered
    primary_key="id",
    embedding_dimension=1024, #Match your model embedding size (bge = 1024, ada = 1536)
    embedding_vector_column="bge_embedding"
  )

# COMMAND ----------

# DBTITLE 1,ADA Vector Search endpoint - one time run
vsc_ada.create_endpoint(name=endpoint_name_ada, endpoint_type="STANDARD")

# COMMAND ----------

# DBTITLE 1,ADA Vector Search Index - one time run
vsc_ada.create_delta_sync_index(
    endpoint_name=endpoint_name_ada,
    index_name=vs_index_fullname_ada,
    source_table_name="demo.hackathon.databricks_pdf_documentation_openai",
    pipeline_type="TRIGGERED", #Sync needs to be manually triggered
    primary_key="id",
    embedding_dimension=1536, #Match your model embedding size (bge = 1024, ada = 1536)
    embedding_vector_column="ada_embedding"
  )

# COMMAND ----------

# DBTITLE 1,Resync BGE Embeddings
# # Resync our index with new data
# vsc_bge.get_index(endpoint_name_bge, vs_index_fullname_bge).sync()

# COMMAND ----------

# DBTITLE 1,Resync ADA Embeddings
# # Resync our index with new data
# vsc_ada.get_index(endpoint_name_ada, vs_index_fullname_ada).sync()

# COMMAND ----------

# DBTITLE 1,Filter documents by State
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

query = f"What rights does the Colorado Privact act grant consumers?"

response = get_state_from_query(query)
cleaned_response = response.replace("```json", "")
cleaned_response = cleaned_response.replace("```", "")
filters = ast.literal_eval(cleaned_response)
print(filters)

# COMMAND ----------

# DBTITLE 1,Ada search function
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

# DBTITLE 1,BGE Search Function
# Ad-hoc BGE embedding function
import mlflow.deployments

bge_deploy_client = mlflow.deployments.get_deploy_client("databricks")

def get_bge_embeddings(query):
    #Note: this will fail if an exception is thrown during embedding creation (add try/except if needed) 
    response = bge_deploy_client.predict(endpoint="databricks-bge-large-en", inputs={"input": query})
    #return [e['embedding'] for e in response.data]
    return response.data[0]['embedding']

# COMMAND ----------

# DBTITLE 1,Run the BGE Search
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

# DBTITLE 1,Combine RAG results
docs = docs_bge + docs_ada
dedup_docs = list(set(tuple(i) for i in docs))
final_list = [list(i) for i in dedup_docs]

print(final_list)
# print(len(docs_bge), len(docs_ada) , len(dedup_docs))


# COMMAND ----------

# DBTITLE 1,Reranking with bge-reranker-large
from FlagEmbedding import FlagReranker
# Load model directly
from transformers import AutoModelForSequenceClassification, AutoTokenizer

tokenizer = AutoTokenizer.from_pretrained("BAAI/bge-reranker-large")
model = AutoModelForSequenceClassification.from_pretrained("BAAI/bge-reranker-large")

reranker = FlagReranker('BAAI/bge-reranker-large', use_fp16=True) # Setting use_fp16 to True speeds up computation with a slight performance degradation

query_and_docs = [[query, d[1]] for d in final_list]

scores = reranker.compute_score(query_and_docs)

reranked_docs = sorted(list(zip(final_list, scores)), key=lambda x: x[1], reverse=True)

pprint(reranked_docs[0])

# COMMAND ----------

# DBTITLE 1,Mixtral - Function to summarize the results
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

# DBTITLE 1,Get the final results!
# print LLM output
print(query)
print(f"\n\n{mixtral_query(userquery)}", 
f"\n\nDocument from State: {reranked_docs[0][0][1]}",
f"\nResult id: {reranked_docs[0][0][0]}",
f"\nDocument path: {reranked_docs[0][0][2]}"
)
