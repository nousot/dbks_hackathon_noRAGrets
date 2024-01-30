# Databricks notebook source
# MAGIC %pip install transformers==4.30.2 "unstructured[pdf,docx]==0.10.30" langchain==0.0.319 llama-index==0.9.3 databricks-vectorsearch==0.20 pydantic==1.10.9 mlflow==2.9.0 protobuf==3.20.0 openai
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %run ./_resources/00-init-advanced $reset_all_data=false

# COMMAND ----------

install_ocr_on_nodes()

# COMMAND ----------

catalog = "demo"
db = "hackathon"
volume_folder = f"/Volumes/{catalog}/{db}/privacy_act_docs/*/"
df = (spark.readStream
        .format('cloudFiles')
        .option('cloudFiles.format', 'BINARYFILE')
        .option("pathGlobFilter", "*.pdf")
        .load('dbfs:'+volume_folder))

# Write the data as a Delta table
(df.writeStream
  .trigger(availableNow=True)
  .option("checkpointLocation", f'dbfs:{volume_folder}/checkpoints/raw_docs')
  .table(f'{catalog}.{db}.pdf_raw').awaitTermination())

# COMMAND ----------

dbutils.secrets.list('dev_demo')

# COMMAND ----------

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

import io
import re

with open("/Volumes/demo/hackathon/privacy_act_docs/Colorado/CPA-regulations.pdf", "rb") as fh:
    bytes_stream = bytes(fh.read())
doc = extract_doc_text(bytes_stream)
print(doc)

# COMMAND ----------

from llama_index.langchain_helpers.text_splitter import SentenceSplitter
from llama_index import Document, set_global_tokenizer
from transformers import AutoTokenizer
from pyspark.sql.functions import pandas_udf
from typing import Iterator
import pandas as pd
import os
import logging
from pyspark.sql import functions as F
import mypy_extensions


# Reduce the arrow batch size as our PDF can be big in memory
spark.conf.set("spark.sql.execution.arrow.maxRecordsPerBatch", 10)

os.environ["HF_HOME"] = '/tmp'

@pandas_udf("array<string>")
def read_as_chunk(batch_iter: Iterator[pd.Series]) -> Iterator[pd.Series]:
    #set llama2 as tokenizer to match our model size (will stay below BGE 1024 limit)
    set_global_tokenizer(
      AutoTokenizer.from_pretrained("hf-internal-testing/llama-tokenizer", cache_dir = '/tmp')
    )
    #Sentence splitter from llama_index to split on sentences
    splitter = SentenceSplitter(chunk_size=50, chunk_overlap=15)
    def extract_and_split(b):
      txt = extract_doc_text(b)
      nodes = splitter.get_nodes_from_documents([Document(text=txt)])
      logging.info(f"from chunk function: {txt}")
      
      return [n.text for n in nodes]

    for x in batch_iter:
        yield x.apply(extract_and_split)

# COMMAND ----------

from mlflow.deployments import get_deploy_client
from pprint import pprint

# bge-large-en Foundation models are available using the /serving-endpoints/databricks-bge-large-en/invocations api. 
deploy_client = get_deploy_client("databricks")

embeddings = deploy_client.predict(endpoint="databricks-bge-large-en", inputs={"input": ["What is Apache Spark?"]})
pprint(embeddings)

# COMMAND ----------

# MAGIC %sql
# MAGIC --Note that we need to enable Change Data Feed on the table to create the index
# MAGIC CREATE TABLE IF NOT EXISTS demo.hackathon.databricks_pdf_documentation (
# MAGIC   id BIGINT GENERATED BY DEFAULT AS IDENTITY,
# MAGIC   url STRING,
# MAGIC   content STRING,
# MAGIC   embedding ARRAY <FLOAT>
# MAGIC ) TBLPROPERTIES (delta.enableChangeDataFeed = true); 

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

from openai import AzureOpenAI

client = AzureOpenAI(
    api_key = dbutils.secrets.get(scope='dev_demo', key='azure_openai_api_key'),
    api_version = "2023-05-15",
    azure_endpoint = "https://nous-ue2-openai-sbx-openai.openai.azure.com/",
    
    )

def open_ai_embeddings(content: str):
    embed_model = "nous-ue2-openai-sbx-base-deploy-text-embedding-ada-002"

    response = client.embeddings.create(
        input = content,
        model = embed_model
    )

    return response.data[0].embedding

# openai_udf = F.udf(lambda x: open_ai_embeddings(x, client), StringType())

# COMMAND ----------

# # Reduce the arrow batch size as our PDF can be big in memory
# spark.conf.set("spark.sql.execution.arrow.maxRecordsPerBatch", 10)

# os.environ["HF_HOME"] = '/tmp'

# set_global_tokenizer(
#   AutoTokenizer.from_pretrained("hf-internal-testing/llama-tokenizer", cache_dir = '/tmp')
# )
# #Sentence splitter from llama_index to split on sentences
# splitter = SentenceSplitter(chunk_size=500, chunk_overlap=50)
# def extract_and_split(b):
#   txt = extract_doc_text(b)
#   nodes = splitter.get_nodes_from_documents([Document(text=txt)])
#   return [n.text for n in nodes]

# col = spark.table('demo.hackathon.pdf_raw')
# pd_col = col.toPandas()

# pd_col["content"].apply(extract_and_split)

volume_folder = f"/Volumes/demo/hackathon/privacy_act_docs/*"
from pyspark.sql.functions import lit

temp = spark.table('demo.hackathon.databricks_pdf_documentation') \
      .withColumn("embedding", lit(open_ai_embeddings("content"))) \
      .selectExpr('url', 'content', 'embedding')

temp.write \
    .option("checkpointLocation", f'dbfs:{volume_folder}/checkpoints/pdf_chunk_openai') \
    .mode("overwrite") \
    .saveAsTable('demo.hackathon.databricks_pdf_documentation_openai')

# (spark.readStream.table('demo.hackathon.pdf_raw')
#       .withColumn("content", F.explode(read_as_chunk("content")))
#       .withColumn("embedding", get_embedding("content"))
#       .selectExpr('path as url', 'content', 'embedding')
#   .writeStream
#     .trigger(availableNow=True)
#     .option("checkpointLocation", f'dbfs:/Volumes/demo/hackathon/privacy_act_docs/Colorado/checkpoints/pdf_chunk')
#     .table('demo.hackathon.databricks_pdf_documentation').awaitTermination())

# #Let's also add our documentation web page from the simple demo (make sure you run the quickstart demo first)
# if table_exists('demo.hackathon.databricks_documentation'):
#   (spark.readStream.table('databricks_documentation')
#       .withColumn('embedding', get_embedding("content"))
#       .select('url', 'content', 'embedding')
#   .writeStream
#     .trigger(availableNow=True)
#     .option("checkpointLocation", f'dbfs:{volume_folder}/checkpoints/docs_chunks')
#     .table('databricks_pdf_documentation').awaitTermination())

# COMMAND ----------

from pyspark.sql import functions as F
import mypy_extensions
from llama_index.langchain_helpers.text_splitter import SentenceSplitter
from llama_index import Document, set_global_tokenizer
from transformers import AutoTokenizer
from pyspark.sql.functions import pandas_udf
from typing import Iterator
import pandas as pd
import os

# # Reduce the arrow batch size as our PDF can be big in memory
# spark.conf.set("spark.sql.execution.arrow.maxRecordsPerBatch", 10)

# os.environ["HF_HOME"] = '/tmp'

# set_global_tokenizer(
#   AutoTokenizer.from_pretrained("hf-internal-testing/llama-tokenizer", cache_dir = '/tmp')
# )
# #Sentence splitter from llama_index to split on sentences
# splitter = SentenceSplitter(chunk_size=500, chunk_overlap=50)
# def extract_and_split(b):
#   txt = extract_doc_text(b)
#   nodes = splitter.get_nodes_from_documents([Document(text=txt)])
#   return [n.text for n in nodes]

# col = spark.table('demo.hackathon.pdf_raw')
# pd_col = col.toPandas()

# pd_col["content"].apply(extract_and_split)

volume_folder = f"/Volumes/demo/hackathon/privacy_act_docs/*"

temp = spark.table('demo.hackathon.pdf_raw') \
      .withColumn("content", F.explode(read_as_chunk("content"))) \
      .withColumn("embedding", get_embedding("content")) \
      .selectExpr('path as url', 'content', 'embedding')

temp.write \
    .option("checkpointLocation", f'dbfs:{volume_folder}/checkpoints/pdf_chunk') \
    .mode("overwrite") \
    .saveAsTable('demo.hackathon.databricks_pdf_documentation')

# (spark.readStream.table('demo.hackathon.pdf_raw')
#       .withColumn("content", F.explode(read_as_chunk("content")))
#       .withColumn("embedding", get_embedding("content"))
#       .selectExpr('path as url', 'content', 'embedding')
#   .writeStream
#     .trigger(availableNow=True)
#     .option("checkpointLocation", f'dbfs:/Volumes/demo/hackathon/privacy_act_docs/Colorado/checkpoints/pdf_chunk')
#     .table('demo.hackathon.databricks_pdf_documentation').awaitTermination())

# #Let's also add our documentation web page from the simple demo (make sure you run the quickstart demo first)
# if table_exists('demo.hackathon.databricks_documentation'):
#   (spark.readStream.table('databricks_documentation')
#       .withColumn('embedding', get_embedding("content"))
#       .select('url', 'content', 'embedding')
#   .writeStream
#     .trigger(availableNow=True)
#     .option("checkpointLocation", f'dbfs:{volume_folder}/checkpoints/docs_chunks')
#     .table('databricks_pdf_documentation').awaitTermination())

# COMMAND ----------

from pyspark.sql import functions as F
import mypy_extensions
from llama_index.langchain_helpers.text_splitter import SentenceSplitter
from llama_index import Document, set_global_tokenizer
from transformers import AutoTokenizer
from pyspark.sql.functions import pandas_udf
from typing import Iterator
import pandas as pd
import os

# # Reduce the arrow batch size as our PDF can be big in memory
# spark.conf.set("spark.sql.execution.arrow.maxRecordsPerBatch", 10)

# os.environ["HF_HOME"] = '/tmp'

# set_global_tokenizer(
#   AutoTokenizer.from_pretrained("hf-internal-testing/llama-tokenizer", cache_dir = '/tmp')
# )
# #Sentence splitter from llama_index to split on sentences
# splitter = SentenceSplitter(chunk_size=500, chunk_overlap=50)
# def extract_and_split(b):
#   txt = extract_doc_text(b)
#   nodes = splitter.get_nodes_from_documents([Document(text=txt)])
#   return [n.text for n in nodes]

# col = spark.table('demo.hackathon.pdf_raw')
# pd_col = col.toPandas()

# pd_col["content"].apply(extract_and_split)

volume_folder = f"/Volumes/demo/hackathon/privacy_act_docs/*"

temp = spark.table('demo.hackathon.pdf_raw') \
      .withColumn("content", F.explode(read_as_chunk("content"))) \
      .withColumn("embedding", get_embedding("content")) \
      .selectExpr('path as url', 'content', 'embedding')

temp.write \
    .option("checkpointLocation", f'dbfs:{volume_folder}/checkpoints/pdf_chunk') \
    .mode("overwrite") \
    .saveAsTable('demo.hackathon.databricks_pdf_documentation')

# (spark.readStream.table('demo.hackathon.pdf_raw')
#       .withColumn("content", F.explode(read_as_chunk("content")))
#       .withColumn("embedding", get_embedding("content"))
#       .selectExpr('path as url', 'content', 'embedding')
#   .writeStream
#     .trigger(availableNow=True)
#     .option("checkpointLocation", f'dbfs:/Volumes/demo/hackathon/privacy_act_docs/Colorado/checkpoints/pdf_chunk')
#     .table('demo.hackathon.databricks_pdf_documentation').awaitTermination())

# #Let's also add our documentation web page from the simple demo (make sure you run the quickstart demo first)
# if table_exists('demo.hackathon.databricks_documentation'):
#   (spark.readStream.table('databricks_documentation')
#       .withColumn('embedding', get_embedding("content"))
#       .select('url', 'content', 'embedding')
#   .writeStream
#     .trigger(availableNow=True)
#     .option("checkpointLocation", f'dbfs:{volume_folder}/checkpoints/docs_chunks')
#     .table('databricks_pdf_documentation').awaitTermination())

# COMMAND ----------

from databricks.vector_search.client import VectorSearchClient
vsc = VectorSearchClient()

VECTOR_SEARCH_ENDPOINT_NAME = "privacy_vector_search"

if VECTOR_SEARCH_ENDPOINT_NAME not in [e['name'] for e in vsc.list_endpoints()['endpoints']]:
    vsc.create_endpoint(name=VECTOR_SEARCH_ENDPOINT_NAME, endpoint_type="STANDARD")

wait_for_vs_endpoint_to_be_ready(vsc, "VECTOR_SEARCH_ENDPOINT_NAME")
print(f"Endpoint named {VECTOR_SEARCH_ENDPOINT_NAME} is ready.")

# COMMAND ----------

from databricks.sdk import WorkspaceClient
import databricks.sdk.service.catalog as c

#The table we'd like to index
source_table_fullname = f"{catalog}.{db}.databricks_pdf_documentation"
# Where we want to store our index
vs_index_fullname = f"{catalog}.{db}.databricks_pdf_documentation_self_managed_vs_index"

if not index_exists(vsc, "privacy_vector_search", vs_index_fullname):
  print(f"Creating index {vs_index_fullname} on endpoint privacy_vector_search...")
  vsc.create_delta_sync_index(
    endpoint_name="privacy_vector_search",
    index_name=vs_index_fullname,
    source_table_name=source_table_fullname,
    pipeline_type="TRIGGERED", #Sync needs to be manually triggered
    primary_key="id",
    embedding_dimension=1024, #Match your model embedding size (bge)
    embedding_vector_column="embedding"
  )
else:
  #Trigger a sync to update our vs content with the new data saved in the table
  vsc.get_index("privacy_vector_search", vs_index_fullname).sync()

#Let's wait for the index to be ready and all our embeddings to be created and indexed
wait_for_index_to_be_ready(vsc, VECTOR_SEARCH_ENDPOINT_NAME, vs_index_fullname)

# COMMAND ----------

question = "When did the colorado privacy act go into effect?"

response = deploy_client.predict(endpoint="databricks-bge-large-en", inputs={"input": [question]})
embeddings = [e['embedding'] for e in response.data]

results = vsc.get_index("vector_search_privacy", vs_index_fullname).similarity_search(
  query_vector=embeddings[0],
  columns=["url", "content"],
  num_results=1)
docs = results.get('result', {}).get('data_array', [])
pprint(docs)
