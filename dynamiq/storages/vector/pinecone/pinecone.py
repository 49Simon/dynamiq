from copy import copy
from typing import TYPE_CHECKING, Any, Optional

from dynamiq.connections import Pinecone, PineconeIndexType
from dynamiq.storages.vector.base import BaseVectorStoreParams, BaseWriterVectorStoreParams
from dynamiq.storages.vector.pinecone.filters import _normalize_filters
from dynamiq.storages.vector.utils import create_file_id_filter
from dynamiq.types import Document
from dynamiq.utils.logger import logger

if TYPE_CHECKING:
    from pinecone import Pinecone as PineconeClient

TOP_K_LIMIT = 1_000


class PineconeVectorStoreParams(BaseVectorStoreParams):
    namespace: str = "default"


class PineconeWriterVectorStoreParams(PineconeVectorStoreParams, BaseWriterVectorStoreParams):
    batch_size: int = 100
    dimension: int = 1536
    metric: str = "cosine"


class PineconeVectorStore:
    """Vector store using Pinecone."""

    def __init__(
        self,
        connection: Pinecone | None = None,
        client: Optional["PineconeClient"] = None,
        index_name: str = "default",
        namespace: str = "default",
        batch_size: int = 100,
        dimension: int = 1536,
        metric: str = "cosine",
        create_if_not_exist: bool = True,
        **index_creation_kwargs,
    ):
        """
        Initialize a PineconeVectorStore instance.

        Args:
            connection (Optional[Pinecone]): Pinecone connection instance. Defaults to None.
            client (Optional[PineconeClient]): Pinecone client instance. Defaults to None.
            index_name (str): Name of the Pinecone index. Defaults to 'default'.
            namespace (str): Namespace for the index. Defaults to 'default'.
            batch_size (int): Size of batches for operations. Defaults to 100.
            dimension (int): Number of dimensions for vectors. Defaults to 1536.
            metric (str): Metric for calculating vector similarity. Defaults to 'cosine'.
            **index_creation_kwargs: Additional arguments for index creation.
        """
        self.connection = connection or Pinecone()
        self.client = client or self.connection.connect()

        self.index_name = index_name
        self.namespace = namespace
        if self.connection.index_type == PineconeIndexType.SERVERLESS:
            self._spec = self.connection.serverless_spec
        else:
            self._spec = self.connection.pod_spec

        self.create_if_not_exist = create_if_not_exist
        self.batch_size = batch_size
        self.metric = metric
        self.index_creation_kwargs = index_creation_kwargs
        self.dimension = dimension
        self._dummy_vector = [-10.0] * dimension
        self._index = self.connect_to_index()
        logger.debug(
            f"PineconeVectorStore initialized with index {self.index_name} and namespace {self.namespace}."
        )

    def connect_to_index(self):
        """
        Create or connect to an existing Pinecone index.

        Returns:
            The initialized Pinecone index object.
        """
        available_indexes = self.client.list_indexes().index_list["indexes"]
        indexes_names = [index["name"] for index in available_indexes]
        if self.index_name not in indexes_names:
            if self.create_if_not_exist:
                logger.debug(f"Index {self.index_name} does not exist. Creating a new index.")
                return self.client.create_index(
                    name=self.index_name,
                    spec=self._spec,
                    dimension=self.dimension,
                    metric=self.metric,
                    **self.index_creation_kwargs,
                )
            else:
                raise ValueError(
                    f"Index {self.index_name} does not exist. Set 'create_if_not_exist' to True to create a new index."
                )
        else:
            logger.debug(f"Index {self.index_name} already exists. Connecting to it.")
            return self.client.Index(name=self.index_name)

    def _set_dimension(self, dimension: int):
        """
        Set the dimension for the index, with a warning if it differs from the actual dimension.

        Args:
            dimension (int): The desired dimension.

        Returns:
            int: The actual dimension of the index.
        """
        actual_dimension = self._index.describe_index_stats().get("dimension")
        if actual_dimension and actual_dimension != dimension:
            logger.warning(
                f"Dimension of index {self.index_name} is {actual_dimension}, but {dimension} was specified. "
                "The specified dimension will be ignored. "
                "If you need an index with a different dimension, please create a new one."
            )
        return actual_dimension or dimension

    def delete_index(self):
        """Delete the entire index."""
        self._index.delete(delete_all=True, namespace=self.namespace)
        self.client.delete_index(self.index_name)

    def delete_documents(self, document_ids: list[str] | None = None, delete_all: bool = False) -> None:
        """
        Delete documents from the Pinecone vector store.

        Args:
            document_ids (list[str]): List of document IDs to delete. Defaults to None.
            delete_all (bool): If True, delete all documents. Defaults to False.
        """
        if delete_all and self._index is not None:
            self._index.delete(delete_all=True, namespace=self.namespace)
            self._index = self.connect_to_index()
        else:
            if not document_ids:
                logger.warning(
                    "No document IDs provided. No documents will be deleted."
                )
            else:
                self._index.delete(ids=document_ids, namespace=self.namespace)

    def delete_documents_by_filters(
        self, filters: dict[str, Any], top_k: int = 1000
    ) -> None:
        """
        Delete documents from the Pinecone vector store using filters.

        Args:
            filters (dict[str, Any]): Filters to select documents to delete.
            top_k (int): Maximum number of documents to retrieve for deletion. Defaults to 1000.
        """
        if self.connection.index_type == PineconeIndexType.SERVERLESS:
            """
            Serverless and Starter indexes do not support deleting with metadata filtering.
            """
            documents = self._embedding_retrieval(
                query_embedding=self._dummy_vector,
                filters=filters,
                exclude_document_embeddings=True,
                top_k=top_k,
            )
            document_ids = [doc.id for doc in documents]
            self.delete_documents(document_ids=document_ids)
        else:
            filters = _normalize_filters(filters)
            self._index.delete(filter=filters, namespace=self.namespace)

    def delete_documents_by_file_id(self, file_id: str):
        """
        Delete documents from the Pinecone vector store by file ID.
            file_id should be located in the metadata of the document.

        Args:
            file_id (str): The file ID to filter by.
        """
        filters = create_file_id_filter(file_id)
        self.delete_documents_by_filters(filters)

    def list_documents(self, include_embeddings: bool = False) -> list[Document]:
        """
        List documents in the Pinecone vector store.

        Args:
            include_embeddings (bool): Whether to include embeddings in the results. Defaults to False.

        Returns:
            list[Document]: List of Document objects retrieved.
        """

        all_documents = []
        for batch_doc_ids in self._index.list(namespace=self.namespace):
            response = self._index.fetch(ids=batch_doc_ids, namespace=self.namespace)

            documents = []
            for pinecone_doc in response["vectors"].values():
                content = pinecone_doc["metadata"].pop("content", None)

                embedding = None
                if include_embeddings and pinecone_doc["values"] != self._dummy_vector:
                    embedding = pinecone_doc["values"]

                doc = Document(
                    id=pinecone_doc["id"],
                    content=content,
                    metadata=pinecone_doc["metadata"],
                    embedding=embedding,
                    score=None,
                )
                documents.append(doc)

            all_documents.extend(documents)
        return all_documents

    def count_documents(self) -> int:
        """
        Count the number of documents in the store.

        Returns:
            int: The number of documents in the store.
        """
        try:
            count = self._index.describe_index_stats()["namespaces"][self.namespace][
                "vector_count"
            ]
        except KeyError:
            count = 0
        return count

    def write_documents(self, documents: list[Document]) -> int:
        """
        Write documents to the Pinecone vector store.

        Args:
            documents (list[Document]): List of Document objects to write.

        Returns:
            int: Number of documents successfully written.

        Raises:
            ValueError: If documents are not of type Document.
        """
        if len(documents) > 0 and not isinstance(documents[0], Document):
            msg = "param 'documents' must contain a list of objects of type Document"
            raise ValueError(msg)

        documents_for_pinecone = self._convert_documents_to_pinecone_format(documents)

        result = self._index.upsert(
            vectors=documents_for_pinecone,
            namespace=self.namespace,
            batch_size=self.batch_size,
        )

        written_docs = result["upserted_count"]
        return written_docs

    def _convert_documents_to_pinecone_format(
        self, documents: list[Document]
    ) -> list[dict[str, Any]]:
        """
        Convert Document objects to Pinecone-compatible format.

        Args:
            documents (list[Document]): List of Document objects to convert.

        Returns:
            list[dict[str, Any]]: List of documents in Pinecone-compatible format.
        """
        documents_for_pinecone = []
        for document in documents:
            embedding = copy(document.embedding)
            if embedding is None:
                logger.warning(
                    f"Document {document.id} has no embedding. A dummy embedding will be used."
                )
                embedding = self._dummy_vector
            doc_for_pinecone = {
                "id": document.id,
                "values": embedding,
                "metadata": dict(document.metadata),
            }

            if document.content is not None:
                doc_for_pinecone["metadata"]["content"] = document.content

            documents_for_pinecone.append(doc_for_pinecone)
        return documents_for_pinecone

    def _embedding_retrieval(
        self,
        query_embedding: list[float],
        *,
        namespace: str | None = None,
        filters: dict[str, Any] | None = None,
        top_k: int = 10,
        exclude_document_embeddings: bool = True,
    ) -> list[Document]:
        """
        Retrieve documents similar to the given query embedding.

        Args:
            query_embedding (list[float]): The query embedding vector.
            namespace (str | None): The namespace to query. Defaults to None.
            filters (dict[str, Any] | None): Filters for the query. Defaults to None.
            top_k (int): Maximum number of documents to retrieve. Defaults to 10.
            exclude_document_embeddings (bool): Whether to exclude embeddings in results. Defaults to True.

        Returns:
            list[Document]: List of retrieved Document objects.

        Raises:
            ValueError: If query_embedding is empty or filter format is incorrect.
        """
        if not query_embedding:
            msg = "query_embedding must be a non-empty list of floats"
            raise ValueError(msg)

        filters = _normalize_filters(filters) if filters else None

        result = self._index.query(
            vector=query_embedding,
            top_k=top_k,
            namespace=namespace or self.namespace,
            filter=filters,
            include_values=not exclude_document_embeddings,
            include_metadata=True,
        )

        return self._convert_query_result_to_documents(result)

    def _convert_query_result_to_documents(
        self, query_result: dict[str, Any]
    ) -> list[Document]:
        """
        Convert Pinecone query results to Document objects.

        Args:
            query_result (dict[str, Any]): The query result from Pinecone.

        Returns:
            list[Document]: List of Document objects created from the query result.
        """
        pinecone_docs = query_result["matches"]
        documents = []
        for pinecone_doc in pinecone_docs:
            content = pinecone_doc["metadata"].pop("content", None)

            embedding = None
            if pinecone_doc["values"] != self._dummy_vector:
                embedding = pinecone_doc["values"]

            doc = Document(
                id=pinecone_doc["id"],
                content=content,
                metadata=pinecone_doc["metadata"],
                embedding=embedding,
                score=pinecone_doc["score"],
            )
            documents.append(doc)

        return documents