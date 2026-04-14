;; RAG Knowledge Base initialization and retrieval

(= (initKnowledge)
   (progn (println! "Initializing knowledge base")
          (println! (py-call (rag.init_knowledge)))))

(= (getKnowledge $msg)
   (py-call (rag.query_knowledge $msg)))
