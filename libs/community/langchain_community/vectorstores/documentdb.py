from __future__ import annotations

import logging
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Generator,
    Iterable,
    List,
    Optional,
    Tuple,
    TypeVar,
    Union,
)

from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStore

if TYPE_CHECKING:
    from langchain_core.embeddings import Embeddings
    from pymongo.collection import Collection


# Before Python 3.11 native StrEnum is not available
class DocumentDBSimilarityType(str, Enum):
    """DocumentDB Similarity Type as enumerator."""

    COS = "cosine"
    """Cosine similarity"""
    DOT = "dotProduct"
    """Dot product"""
    EUC = "euclidean"
    """Euclidean distance"""


DocumentDBDocumentType = TypeVar("DocumentDBDocumentType", bound=Dict[str, Any])

logger = logging.getLogger(__name__)

DEFAULT_INSERT_BATCH_SIZE = 128


class DocumentDBVectorSearch(VectorStore):
    """`Amazon DocumentDB (with MongoDB compatibility)` vector store.
    Please refer to the official Vector Search documentation for more details:
    https://docs.aws.amazon.com/documentdb/latest/developerguide/vector-search.html

    To use, you should have both:
    - the ``pymongo`` python package installed
    - a connection string and credentials associated with a DocumentDB cluster

    Example:
        . code-block:: python

            from langchain_community.vectorstores import DocumentDBVectorSearch
            from langchain_community.embeddings.openai import OpenAIEmbeddings
            from pymongo import MongoClient

            mongo_client = MongoClient("<YOUR-CONNECTION-STRING>")
            collection = mongo_client["<db_name>"]["<collection_name>"]
            embeddings = OpenAIEmbeddings()
            vectorstore = DocumentDBVectorSearch(collection, embeddings)
    """

    def __init__(
        self,
        collection: Collection[DocumentDBDocumentType],
        embedding: Embeddings,
        *,
        index_name: str = "vectorSearchIndex",
        text_key: str = "textContent",
        embedding_key: str = "vectorContent",
    ):
        """Constructor for DocumentDBVectorSearch

        Args:
            collection: MongoDB collection to add the texts to.
            embedding: Text embedding model to use.
            index_name: Name of the Vector Search index.
            text_key: MongoDB field that will contain the text
                for each document.
            embedding_key: MongoDB field that will contain the embedding
                for each document.
        """
        self._collection = collection
        self._embedding = embedding
        self._index_name = index_name
        self._text_key = text_key
        self._embedding_key = embedding_key

    @property
    def embeddings(self) -> Embeddings:
        return self._embedding

    def get_index_name(self) -> str:
        """Returns the index name

        Returns:
            Returns the index name

        """
        return self._index_name

    @classmethod
    def from_connection_string(
        cls,
        connection_string: str,
        namespace: str,
        embedding: Embeddings,
        **kwargs: Any,
    ) -> DocumentDBVectorSearch:
        """Creates an Instance of DocumentDBVectorSearch from a Connection String

        Args:
            connection_string: The DocumentDB cluster endpoint connection string
            namespace: The namespace (database.collection)
            embedding: The embedding utility
            **kwargs: Dynamic keyword arguments

        Returns:
            an instance of the vector store

        """
        try:
            from pymongo import MongoClient
        except ImportError:
            raise ImportError(
                "Could not import pymongo, please install it with "
                "`pip install pymongo`."
            )
        client: MongoClient = MongoClient(connection_string)
        db_name, collection_name = namespace.split(".")
        collection = client[db_name][collection_name]
        return cls(collection, embedding, **kwargs)

    def index_exists(self) -> bool:
        """Verifies if the specified index name during instance
            construction exists on the collection

        Returns:
          Returns True on success and False if no such index exists
            on the collection
        """
        cursor = self._collection.list_indexes()
        index_name = self._index_name

        for res in cursor:
            current_index_name = res.pop("name")
            if current_index_name == index_name:
                return True

        return False

    def delete_index(self) -> None:
        """Deletes the index specified during instance construction if it exists"""
        if self.index_exists():
            self._collection.drop_index(self._index_name)
            # Raises OperationFailure on an error (e.g. trying to drop
            # an index that does not exist)

    def create_index(
        self,
        lists: int = 100,
        dimensions: int = 1536,
        similarity: DocumentDBSimilarityType = DocumentDBSimilarityType.COS,
    ) -> dict[str, Any]:
        """Creates an index using the index name specified at
            instance construction

        Setting the lists parameter correctly is important for achieving
            good accuracy and performance.
            Since the vector store uses IVFFLAT as the indexing strategy,
            you should create the index only after you
            have loaded a large enough sample of documents to ensure that the
            centroids for the respective buckets are
            fairly distributed.

        We recommend that lists is set to documentCount/1000 for up
            to 1 million documents
            and to sqrt(documentCount) for more than 1 million documents.
            As the number of items in your database grows, you should
            tune lists to be larger
            in order to achieve good latency performance for vector search.

            If you're experimenting with a new scenario or creating a
            small demo, you can start with lists set to 1.
            This should provide you with the most
            accurate results from the vector search, however be aware that
            the search speed and latency will be slow.
            After your initial setup, you should go ahead and tune
            the lists parameter using the above guidance.
        NOTE: There are some limitations for lists values based on the instance type.
            Please refer to the official documentation here:
            https://docs.aws.amazon.com/documentdb/latest/developerguide/vector-search.html#vector-limitations

        Args:
            lists: This integer is the number of clusters that the
                inverted file (IVFFLAT) index uses to group the vector data.
                We recommend that lists is set to documentCount/1000
                for up to 1 million documents and to sqrt(documentCount)
                for more than 1 million documents.
            dimensions: Number of dimensions for vector similarity.
                The maximum number of supported dimensions is 2000
            similarity: Similarity algorithm to use with the IVFFLAT index.

                Possible options are:
                    - DocumentDBSimilarityType.COS (cosine distance),
                    - DocumentDBSimilarityType.EUC (Euclidean distance), and
                    - DocumentDBSimilarityType.DOT (dot product).

        Returns:
            An object describing the created index

        """
        # prepare the command
        create_index_commands = {
            "createIndexes": self._collection.name,
            "indexes": [
                {
                    "name": self._index_name,
                    "key": {self._embedding_key: "vector"},
                    "vectorOptions": {
                        "lists": lists,
                        "similarity": similarity,
                        "dimensions": dimensions,
                    },
                }
            ],
        }

        # retrieve the database object
        current_database = self._collection.database

        # invoke the command from the database object
        create_index_responses: dict[str, Any] = current_database.command(
            create_index_commands
        )

        return create_index_responses

    def add_texts(
        self,
        texts: Iterable[str],
        metadatas: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> List:
        batch_size = kwargs.get("batch_size", DEFAULT_INSERT_BATCH_SIZE)
        _metadatas: Union[List, Generator] = metadatas or ({} for _ in texts)
        texts_batch = []
        metadatas_batch = []
        result_ids = []
        for i, (text, metadata) in enumerate(zip(texts, _metadatas)):
            texts_batch.append(text)
            metadatas_batch.append(metadata)
            if (i + 1) % batch_size == 0:
                result_ids.extend(self._insert_texts(texts_batch, metadatas_batch))
                texts_batch = []
                metadatas_batch = []
        if texts_batch:
            result_ids.extend(self._insert_texts(texts_batch, metadatas_batch))
        return result_ids

    def _insert_texts(self, texts: List[str], metadatas: List[Dict[str, Any]]) -> List:
        """Used to Load Documents into the collection

        Args:
            texts: The list of documents strings to load
            metadatas: The list of metadata objects associated with each document

        Returns:

        """
        # If the text is empty, then exit early
        if not texts:
            return []

        # Embed and create the documents
        embeddings = self._embedding.embed_documents(texts)
        to_insert = [
            {self._text_key: t, self._embedding_key: embedding, **m}
            for t, m, embedding in zip(texts, metadatas, embeddings)
        ]
        # insert the documents in DocumentDB
        insert_result = self._collection.insert_many(to_insert)  # type: ignore
        return insert_result.inserted_ids

    @classmethod
    def from_texts(
        cls,
        texts: List[str],
        embedding: Embeddings,
        metadatas: Optional[List[dict]] = None,
        collection: Optional[Collection[DocumentDBDocumentType]] = None,
        **kwargs: Any,
    ) -> DocumentDBVectorSearch:
        if collection is None:
            raise ValueError("Must provide 'collection' named parameter.")
        vectorstore = cls(collection, embedding, **kwargs)
        vectorstore.add_texts(texts, metadatas=metadatas)
        return vectorstore

    def delete(self, ids: Optional[List[str]] = None, **kwargs: Any) -> Optional[bool]:
        if ids is None:
            raise ValueError("No document ids provided to delete.")

        for document_id in ids:
            self.delete_document_by_id(document_id)
        return True

    def delete_document_by_id(self, document_id: Optional[str] = None) -> None:
        """Removes a Specific Document by Id

        Args:
            document_id: The document identifier
        """
        try:
            from bson.objectid import ObjectId
        except ImportError as e:
            raise ImportError(
                "Unable to import bson, please install with `pip install bson`."
            ) from e
        if document_id is None:
            raise ValueError("No document id provided to delete.")

        self._collection.delete_one({"_id": ObjectId(document_id)})

    def _similarity_search_without_score(
        self, embeddings: List[float], similarity: DocumentDBSimilarityType, k: int = 4
    ) -> List[Tuple[Document, float]]:
        """Returns a list of documents.

        Args:
            embeddings: The query vector
            k: the number of documents to return

        Returns:
            A list of documents closest to the query vector
        """
        pipeline: List[dict[str, Any]] = [
            {
                "$search": {
                    "vectorSearch": {
                        "vector": embeddings,
                        "path": self._embedding_key,
                        "similarity": similarity,
                        "k": k,
                    }
                }
            }
        ]

        cursor = self._collection.aggregate(pipeline)

        docs = []

        for res in cursor:
            print(res)
            text = res.pop(self._text_key)
            docs.append(Document(page_content=text, metadata=res))

        return docs

    def similarity_search(
        self,
        query: str,
        similarity: DocumentDBSimilarityType,
        k: int = 4,
        **kwargs: Any,
    ) -> List[Document]:
        embeddings = self._embedding.embed_query(query)
        docs = self._similarity_search_without_score(
            embeddings=embeddings, similarity=similarity, k=k
        )
        return [doc for doc in docs]
