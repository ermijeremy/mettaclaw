(= (getSkills)
       (;DEFAULT ALLOWED SKILLS:
        "- Remember a particular string: (remember string)"
        "- Query long-term embedding memory: (query string)"
        "- Pin a short-term working memory item: (pin string)"
        "- Execute shell command: (shell string)"
        "- Read file to string: (read-file filename)"
        "- Write string to file: (write-file filename string)"
        "- Append line to file: (append-file filename string)"
        "- Send message to user: (send string)"
        "- Search the web: (search string)"
        "- Execute MeTTa expression: (metta string)"
        "- Example to invoke Non-Axiomatic Logic via MeTTa: "
               "- (metta (|- ((--> (× sam garfield) friend) (stv 1.0 0.9))"
               "-            ((--> garfield animal) (stv 1.0 0.9))))"
               "- (metta (|- ((==> (--> (× $1 elephant) eat) (--> $1 ([] dangerous))) (stv 1.0 0.9))"
               "-            ((--> (× tiger elephant) eat) (stv 1.0 0.9))))"
               "- Also: note the $1 for independent variables, and for negated knowledge use (stv 0.0 0.9)"
               "- Additionally |- also works for revision, to merge evidence even when the term of both premises is the same."))

(= (read-file $file)
      (progn (translatePredicate (exists_file $file))
              (translatePredicate (read_file_to_string $file $content ()))
              $content))

(= (write-file $file $str)
   (progn (translatePredicate (open $file write $Out))
              (translatePredicate (write $Out $str))
              (translatePredicate (close $Out))
              True))

(= (append-file $file $str)
      (progn (translatePredicate (exists_file $file))
              (translatePredicate (open $file append $Out))
              (translatePredicate (write $Out $str))
              (translatePredicate (nl $Out)),
              (translatePredicate (close $Out))
              True))

!(import_prolog_functions_from_file (library mettaclaw ./src/skills.pl) (run_cmd first_char))

(= (shell $cmd)
   (let $temp (cut) (translatePredicate (run_cmd $cmd $out)) $out))

(= (metta $str)
   (let $code (sread $str) (eval $code)))
