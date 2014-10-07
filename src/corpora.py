#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# [The "New BSD" license]
# Copyright (c) 2014 The Board of Trustees of The University of Alabama
# All rights reserved.
#
# See LICENSE for details.

"""
Code for generating the corpora.
"""

from StringIO import StringIO
import re
import subprocess
import os.path
import tempfile

import gensim
import dulwich
import dulwich.objects
import dulwich.index
import dulwich.repo
import dulwich.patch

import preprocessing
from errors import TaserError

import logging
logger = logging.getLogger('cfl.corpora')


class GeneralCorpus(gensim.interfaces.CorpusABC):
    def __init__(self, project=None, id2word=None, remove_stops=True, split=True, lower=True,
                 min_len=3, max_len=40, lazy_dict=False):
        lazystr = str()
        if lazy_dict:
            lazystr = 'lazy '

        logger.info('Creating %s%s corpus', lazystr, self.__class__.__name__)

        self.project = project
        self.remove_stops = remove_stops
        self.split = split
        self.lower = lower
        self.min_len = min_len
        self.max_len = max_len
        self.lazy_dict = lazy_dict

        if id2word is None:
            id2word = gensim.corpora.Dictionary()
            logger.info('[gen] Creating new dictionary %s for %s %s',
                        id(id2word), self.__class__.__name__, id(self))
        else:
            logger.info('[gen] Using provided dictionary %s for %s %s',
                        id(id2word), self.__class__.__name__, id(self))

        self.id2word = id2word

        self.metadata = False
        self.label = 'general'

        if not lazy_dict:
            # build the dict (not lazy)
            self.id2word.add_documents(self.get_texts())

        super(GeneralCorpus, self).__init__()

    @property
    def id2word(self):
        return self._id2word

    @id2word.setter
    def id2word(self, val):
        logger.info('[gen] Updating dictionary %s for %s %s', id(val),
                    self.__class__.__name__, id(self))
        self._id2word = val

    def preprocess(self, document, info=[]):
        document = preprocessing.to_unicode(document, info)
        words = preprocessing.tokenize(document)

        if self.split:
            words = preprocessing.split(words)

        if self.lower:
            words = (word.lower() for word in words)

        if self.remove_stops:
            words = preprocessing.remove_stops(words, preprocessing.FOX_STOPS)
            words = preprocessing.remove_stops(words, preprocessing.JAVA_RESERVED)

        def include(word):
            return len(word) >= self.min_len and len(word) <= self.max_len
        words = (word for word in words if include(word))
        return words

    def __iter__(self):
        """
        The function that defines a corpus.

        Iterating over the corpus must yield sparse vectors, one for each
        document.
        """
        for text in self.get_texts():
            if self.metadata:
                meta = text[1]
                text = text[0]
                yield (self.id2word.doc2bow(text, allow_update=self.lazy_dict),
                       meta)
            else:
                yield self.id2word.doc2bow(text, allow_update=self.lazy_dict)

    def __len__(self):
        return self.length  # will throw if corpus not initialized


class GitCorpus(GeneralCorpus):
    def __init__(self, repo, project=None, remove_stops=True, split=True,
                 lower=True, min_len=3, max_len=40, id2word=None, lazy_dict=False):

        if project:
            ref = project.ref
        else:
            ref = 'HEAD'

        logger.info('[git] Creating %s corpus out of source files for commit %s: %s',
                    self.__class__.__name__, ref, str(lazy_dict))
        self.repo = repo

        # ensure ref is a str otherwise dulwich cries
        if isinstance(ref, unicode):
            self.ref = ref.encode('utf-8')
        else:
            self.ref = ref

        self.ref_tree = None

        # find which file tree is for the commit we care about
        self.ref_obj = self.repo[self.ref]
        if isinstance(self.ref_obj, dulwich.objects.Tag):
            self.ref_tree = self.repo[self.ref_obj.object[1]].tree
        elif isinstance(self.ref_obj, dulwich.objects.Commit):
            self.ref_tree = self.ref_obj.tree
        elif isinstance(self.ref_obj, dulwich.objects.Tree):
            self.ref_tree = self.ref_obj.id
        else:
            self.ref_tree = self.ref  # here goes nothin?

        super(GitCorpus, self).__init__(project=project,
                                        remove_stops=remove_stops,
                                        split=split,
                                        lower=lower,
                                        min_len=min_len,
                                        max_len=max_len,
                                        id2word=id2word,
                                        lazy_dict=lazy_dict)

        # set the label
        # filter to get rid of all empty strings
        self.label = filter(lambda x: x, repo.path.split('/'))[-1]


class SnapshotCorpus(GitCorpus):
    def get_texts(self):
        length = 0

        for entry in self.repo.object_store.iter_tree_contents(self.ref_tree):
            fname = entry.path
            document = self.repo.object_store.get_raw(entry.sha)[1]
            if dulwich.patch.is_binary(document):
                continue

            words = self.preprocess(document, [fname, self.ref])
            length += 1

            if self.metadata:
                yield words, (fname, self.label)
            else:
                yield words

        self.length = length  # only reset after iteration is done.


class TaserMixIn(object):
    def run_taser(self):
        self.corpus_generated = False

        cmds = list()
        cmds.append(['java',
                     '-jar', self.taser_jar,
                     'ex', self.src,
                     '-o', self.dest,
                     '-t', self.project.level])
        cmds.append(['java',
                     '-jar', self.taser_jar,
                     'rw', self.dest,
                     '-o', self.dest,
                     '-t', self.project.level])
        cmds.append(['java',
                     '-jar', self.taser_jar,
                     'bc', self.dest,
                     '-o', self.dest,
                     '-t', self.project.level])
        # do not need to do pp since we will preproccess ourselves
        # cmds.append(['java',
        #              '-jar', self.taser_jar,
        #              'pp', self.dest,
        #              '-o', self.dest,
        #              '-t', self.project.level])
        cmds.append(['java',
                     '-jar', self.taser_jar,
                     'fc', self.dest,
                     '-o', self.dest,
                     '-t', self.project.level])

        for cmd in cmds:
            retval = subprocess.call(cmd)
            if retval:
                raise TaserError("Failed cmd: " + str(cmd))

        self.corpus_generated = True

    def get_texts(self):
        if not self.corpus_generated:
            self.run_taser()

        length = 0

        with open(os.path.join(self.dest, 'unknown-0.0.ser')) as f:
            for line in f:
                doc_name, document = line.split(' ', 1)
                words = self.preprocess(document, [doc_name, self.ref])
                length += 1

                if self.metadata:
                    yield words, (doc_name, self.label)
                else:
                    yield words

        self.length = length  # only reset after iteration is done.



class TaserSnapshotCorpus(GitCorpus, TaserMixIn):
    def __init__(self, repo=None, project=None, remove_stops=True, split=True,
                 lower=True, min_len=3, max_len=40, taser_jar='lib/taser.jar',
                 id2word=None, lazy_dict=True):
        # force lazy_dict since we have to run taser to build the corpus first
        super(TaserSnapshotCorpus, self).__init__(repo=repo,
                                                  project=project,
                                                  remove_stops=remove_stops,
                                                  split=split,
                                                  lower=lower,
                                                  min_len=min_len,
                                                  max_len=max_len,
                                                  id2word=id2word,
                                                  lazy_dict=True)
        self.taser_jar = taser_jar

        self.src = tempfile.mkdtemp(prefix='taser_')
        self.dest = tempfile.mkdtemp(prefix='taser_')

        # checkout the version we want
        dulwich.index.build_index_from_tree(self.src,
                                            self.repo.index_path(),
                                            self.repo.object_store,
                                            self.ref_tree)
        self.corpus_generated = False
        self.run_taser()

class TaserReleaseCorpus(GeneralCorpus, TaserMixIn):
    def __init__(self, project, remove_stops=True, split=True, lower=True,
                 min_len=3, max_len=40, taser_jar='lib/taser.jar',
                 id2word=None, lazy_dict=True):
        # force lazy_dict since we have to run taser to build the corpus first
        super(TaserReleaseCorpus, self).__init__(project=project,
                                                 remove_stops=remove_stops,
                                                 split=split,
                                                 lower=lower,
                                                 min_len=min_len,
                                                 max_len=max_len,
                                                 id2word=id2word,
                                                 lazy_dict=True)
        self.taser_jar = taser_jar

        self.src = project.src_path
        self.dest = tempfile.mkdtemp(prefix='taser_')
        self.label = 'release'

        self.corpus_generated = False
        self.run_taser()

    def run_taser(self):
        self.corpus_generated = False

        cmds = list()
        cmds.append(['java', '-jar', self.taser_jar, 'ex', self.src, '-o', self.dest])
        cmds.append(['java', '-jar', self.taser_jar, 'rw', self.dest, '-o', self.dest])
        cmds.append(['java', '-jar', self.taser_jar, 'bc', self.dest, '-o', self.dest])
        # do not need to do pp since we will preproccess ourselves
        # cmds.append(['java', '-jar', self.taser_jar, 'pp', self.dest, '-o', self.dest])
        cmds.append(['java', '-jar', self.taser_jar, 'fc', self.dest, '-o', self.dest])

        for cmd in cmds:
            retval = subprocess.call(cmd)
            if retval:
                raise TaserError("Failed cmd: " + str(cmd))

        self.corpus_generated = True

    def get_texts(self):
        if not self.corpus_generated:
            self.run_taser()

        length = 0

        with open(os.path.join(self.dest, 'unknown-0.0.ser')) as f:
            for line in f:
                doc_name, document = line.split(' ', 1)
                words = self.preprocess(document, [doc_name, self.src])
                length += 1

                if self.metadata:
                    yield words, (doc_name, self.label)
                else:
                    yield words

        self.length = length  # only reset after iteration is done.


class ChangesetCorpus(GitCorpus):
    def _get_diff(self, changeset):
        """ Return a text representing a `git diff` for the files in the
        changeset.

        """
        patch_file = StringIO()
        dulwich.patch.write_object_diff(patch_file,
                                        self.repo.object_store,
                                        changeset.old, changeset.new)
        return patch_file.getvalue()

    def _walk_changes(self, reverse=False):
        """ Returns one file change at a time, not the entire diff.

        """

        for walk_entry in self.repo.get_walker(reverse=reverse):
            commit = walk_entry.commit

            # initial revision, has no parent
            if len(commit.parents) == 0:
                for changes in dulwich.diff_tree.tree_changes(
                        self.repo.object_store, None, commit.tree
                ):
                    diff = self._get_diff(changes)
                    yield commit.id, None, diff

            for parent in commit.parents:
                # do I need to know the parent id?

                for changes in dulwich.diff_tree.tree_changes(
                    self.repo.object_store, self.repo[parent].tree, commit.tree
                ):
                    diff = self._get_diff(changes)
                    yield commit.id, parent, diff

    def get_texts(self):
        length = 0
        unified = re.compile(r'^[+ -].*')
        current = None
        low = list()  # collecting the list of words

        for commit, parent, diff in self._walk_changes():
            # write out once all diff lines for commit have been collected
            # this is over all parents and all files of the commit
            if current is None:
                # set current for the first commit, clear low
                current = commit
                low = list()
            elif current != commit:
                # new commit seen, yield the collected low
                if self.metadata:
                    yield low, (current, self.label)
                else:
                    yield low

                length += 1
                current = commit
                low = list()

            # to process out whitespace only changes, the rest of this
            # loop will need to be structured differently. possibly need
            # to actually parse the diff to gain structure knowledge
            # (ie, line numbers of the changes).

            diff_lines = filter(lambda x: unified.match(x),
                                diff.splitlines())
            if len(diff_lines) < 2:
                continue  # useful for not worrying with binary files

            # sanity?
            assert diff_lines[0].startswith('--- '), diff_lines[0]
            assert diff_lines[1].startswith('+++ '), diff_lines[1]
            # parent_fn = diff_lines[0][4:]
            # commit_fn = diff_lines[1][4:]

            lines = diff_lines[2:]  # chop off file names idtag rebel
            lines = [line[1:] for line in lines]  # remove unified markers
            document = ' '.join(lines)

            # call the tokenizer
            words = self.preprocess(document,
                                    [commit, str(parent), diff_lines[0]])
            low.extend(words)

        length += 1
        if self.metadata:
            # have reached the end, yield whatever was collected last
            yield low, (current, self.label)
        else:
            yield low

        self.length = length  # only reset after iteration is done.


class CommitLogCorpus(GitCorpus):
    def get_texts(self):
        length = 0

        for walk_entry in self.repo.get_walker():
            commit = walk_entry.commit
            words = self.preprocess(commit.message, [commit.id])

            length += 1
            if self.metadata:
                # have reached the end, yield whatever was collected last
                yield words, (commit.id, self.label)
            else:
                yield words

        self.length = length  # only reset after iteration is done.


class CorpusCombiner(GeneralCorpus):
    def __init__(self, corpora=None, id2word=None):
        self.corpora = list()

        super(CorpusCombiner, self).__init__(id2word=id2word, lazy_dict=True)

        if corpora:
            for each in corpora:
                self.add(each)

    def add(self, corpus):
        trans = self.id2word.merge_with(corpus.id2word)
        corpus.metadata = self.metadata
        corpus.id2word = self.id2word
        corpus.word2id = self.id2word.token2id
        self.corpora.append(corpus)

    def __iter__(self):
        for corpus in self.corpora:
            for doc in corpus:
                yield doc

    def __len__(self):
        return sum(len(c) for c in self.corpora)

    @property
    def metadata(self):
        return self._metadata

    @metadata.setter
    def metadata(self, val):
        assert val is True or val is False
        self._metadata = val
        for corpus in self.corpora:
            corpus.metadata = self._metadata

    @property
    def id2word(self):
        return self._id2word

    @id2word.setter
    def id2word(self, val):
        logger.info('[com] Updating dictionary %s for %s %s', id(val),
                    self.__class__.__name__, id(self))
        self._id2word = val
        for corpus in self.corpora:
            corpus.id2word = val
