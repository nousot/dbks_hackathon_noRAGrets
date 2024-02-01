# Databricks notebook source
# MAGIC %pip install transformers==4.30.2 "unstructured[pdf,docx]==0.10.30" llama-index==0.9.40 databricks-vectorsearch==0.20 pydantic==1.10.9 mlflow==2.9.0 protobuf==3.20.0 openai==1.10.0 langchain-openai langchain
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Helper function for read_from_text
from unstructured.partition.auto import partition
import re
import io

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

# DBTITLE 1,OpenAI Client and read as chunk function defined
from llama_index.langchain_helpers.text_splitter import SentenceSplitter
from llama_index.node_parser import SemanticSplitterNodeParser
from llama_index import Document, set_global_tokenizer
from transformers import AutoTokenizer
from pyspark.sql.functions import pandas_udf
from typing import Iterator
import pandas as pd
import os
import logging
from pyspark.sql import functions as F
import mypy_extensions
from openai import AzureOpenAI
from langchain_openai import AzureOpenAIEmbeddings

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
# MAGIC %sql
# MAGIC --Note that we need to enable Change Data Feed on the table to create the index
# MAGIC CREATE TABLE IF NOT EXISTS demo.hackathon.databricks_pdf_documentation_openai (
# MAGIC   id BIGINT GENERATED BY DEFAULT AS IDENTITY,
# MAGIC   url STRING,
# MAGIC   content STRING,
# MAGIC   embedding ARRAY <FLOAT>
# MAGIC ) TBLPROPERTIES (delta.enableChangeDataFeed = true); 

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

# DBTITLE 1,Write to databricks_pdf_documentation_openai
from pyspark.sql import functions as F
import mypy_extensions
import pandas as pd
import os

# # Reduce the arrow batch size as our PDF can be big in memory
# spark.conf.set("spark.sql.execution.arrow.maxRecordsPerBatch", 10)

# os.environ["HF_HOME"] = '/tmp'

volume_folder = f"/Volumes/demo/hackathon/privacy_act_docs/*"

temp = (spark.table('demo.hackathon.pdf_raw')
        .withColumn("content", F.explode(read_as_chunk("content")))
        .withColumn("embedding", F.lit(open_ai_embeddings("content")))
        .withColumn("id", F.monotonically_increasing_id())
        .selectExpr('id', 'path as url', 'content', 'embedding')
        )

(temp.write
    .option("checkpointLocation", f'dbfs:{volume_folder}/checkpoints/pdf_chunk_openai')
    .option("overwriteSchema", "true")
    .mode("overwrite")
    .saveAsTable('demo.hackathon.databricks_pdf_documentation_openai'))

# COMMAND ----------

# DBTITLE 1,Definitions for vector search
from databricks.vector_search.client import VectorSearchClient
vsc = VectorSearchClient()
vs_index_fullname = "demo.hackathon.openai_self_managed_index_v2"
endpoint_name = "openai_vector_search_v2"

# COMMAND ----------

# DBTITLE 1,No need to run again (already created)
# vsc.create_endpoint(name=endpoint_name, endpoint_type="STANDARD")
vsc.create_delta_sync_index(
    endpoint_name=endpoint_name,
    index_name=vs_index_fullname,
    source_table_name="demo.hackathon.databricks_pdf_documentation_openai",
    pipeline_type="TRIGGERED", #Sync needs to be manually triggered
    primary_key="id",
    embedding_dimension=1536, #Match your model embedding size (bge)
    embedding_vector_column="embedding"
  )

# COMMAND ----------

# DBTITLE 1,Run this to resync our index table with new results
# Resync our index with new data
vsc.get_index(endpoint_name, vs_index_fullname).sync()

# COMMAND ----------

# DBTITLE 1,Test prompts (call embedding endpoint here)

