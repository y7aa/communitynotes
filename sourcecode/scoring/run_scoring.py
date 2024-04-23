"""This file drives the scoring and user reputation logic for Community Notes.

This file defines "run_scoring" which invokes all Community Notes scoring algorithms,
merges results and computes contribution statistics for users.  run_scoring should be
intergrated into main files for execution in internal and external environments.
"""
import concurrent.futures
from itertools import chain
import multiprocessing
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

from . import constants as c, contributor_state, note_ratings, note_status_history, scoring_rules
from .constants import FinalScoringArgs, ModelResult, PrescoringArgs, ScoringArgs
from .enums import Scorers, Topics
from .matrix_factorization.normalized_loss import NormalizedLossHyperparameters
from .mf_core_scorer import MFCoreScorer
from .mf_expansion_plus_scorer import MFExpansionPlusScorer
from .mf_expansion_scorer import MFExpansionScorer
from .mf_group_scorer import (
  MFGroupScorer,
  coalesce_group_models,
  groupScorerCount,
  trialScoringGroup,
)
from .mf_topic_scorer import MFTopicScorer, coalesce_topic_models
from .process_data import CommunityNotesDataLoader
from .reputation_scorer import ReputationScorer
from .scorer import Scorer
from .scoring_rules import RuleID
from .topic_model import TopicModel

import numpy as np
import pandas as pd


def _get_scorers(
  seed: Optional[int],
  pseudoraters: Optional[bool],
  enabledScorers: Optional[Set[Scorers]],
  useStableInitialization: bool = True,
) -> Dict[Scorers, List[Scorer]]:
  """Instantiate all Scorer objects which should be used for note ranking.

  Args:
    seed (int, optional): if not None, base distinct seeds for the first and second MF rounds on this value
    pseudoraters (bool, optional): if True, compute optional pseudorater confidence intervals
    enabledScorers: if not None, set of which scorers should be instantiated and enabled

  Returns:
    Dict[Scorers, List[Scorer]] containing instantiated Scorer objects for note ranking.
  """
  scorers: Dict[Scorers, List[Scorer]] = dict()

  if enabledScorers is None or Scorers.MFCoreScorer in enabledScorers:
    scorers[Scorers.MFCoreScorer] = [
      MFCoreScorer(seed, pseudoraters, useStableInitialization=useStableInitialization, threads=12)
    ]
  if enabledScorers is None or Scorers.MFExpansionScorer in enabledScorers:
    scorers[Scorers.MFExpansionScorer] = [
      MFExpansionScorer(seed, useStableInitialization=useStableInitialization, threads=12)
    ]
  if enabledScorers is None or Scorers.MFExpansionPlusScorer in enabledScorers:
    scorers[Scorers.MFExpansionPlusScorer] = [
      MFExpansionPlusScorer(seed, useStableInitialization=useStableInitialization, threads=12)
    ]
  if enabledScorers is None or Scorers.ReputationScorer in enabledScorers:
    scorers[Scorers.ReputationScorer] = [
      ReputationScorer(seed, useStableInitialization=useStableInitialization, threads=12)
    ]
  if enabledScorers is None or Scorers.MFGroupScorer in enabledScorers:
    # Note that index 0 is reserved, corresponding to no group assigned, so scoring group
    # numbers begin with index 1.
    scorers[Scorers.MFGroupScorer] = [
      # Scoring Group 13 is currently the largest by far, so total runtime benefits from
      # adding the group scorers in descending order so we start work on Group 13 first.
      MFGroupScorer(groupNumber=i, seed=seed)
      for i in range(groupScorerCount, 0, -1)
      if i != trialScoringGroup
    ]
    scorers[Scorers.MFGroupScorer].append(
      MFGroupScorer(
        groupNumber=trialScoringGroup,
        seed=seed,
        noteInterceptLambda=0.03 * 30,
        userInterceptLambda=0.03 * 5,
        globalInterceptLambda=0.03 * 5,
        noteFactorLambda=0.03 / 3,
        userFactorLambda=0.03 / 4,
        diamondLambda=0.03 * 25,
        normalizedLossHyperparameters=NormalizedLossHyperparameters(
          globalSignNorm=True, noteSignAlpha=None, noteNormExp=0, raterNormExp=-0.25
        ),
        maxFinalMFTrainError=0.16,
        requireInternalAuthor=False,
        groupThreshold=0.4,
        minMeanNoteScore=-0.01,
        crhThreshold=0.09,
        crhSuperThreshold=0.2,
        crnhThresholdIntercept=-0.01,
        crnhThresholdNoteFactorMultiplier=0,
        crnhThresholdNMIntercept=-0.02,
        lowDiligenceThreshold=1000,
        factorThreshold=0.4,
        multiplyPenaltyByHarassmentScore=False,
        minimumHarassmentScoreToPenalize=2.5,
        tagConsensusHarassmentHelpfulRatingPenalty=10,
      )
    )
  if enabledScorers is None or Scorers.MFTopicScorer in enabledScorers:
    scorers[Scorers.MFTopicScorer] = [
      MFTopicScorer(topicName=topic.name, seed=seed) for topic in Topics
    ]

  return scorers


def _merge_results(
  scoredNotes: pd.DataFrame,
  helpfulnessScores: pd.DataFrame,
  auxiliaryNoteInfo: pd.DataFrame,
  modelScoredNotes: pd.DataFrame,
  modelHelpfulnessScores: Optional[pd.DataFrame],
  modelauxiliaryNoteInfo: Optional[pd.DataFrame],
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
  """Merges results from a specific model with results from prior models.

  The DFs returned by each model will be (outer) merged and passed through directly to the
  return value of run_scoring.  Column names must be unique in each DF with the exception of
  noteId or raterParticipantId, which are used to conduct the merge.

  Args:
    scoredNotes: pd.DataFrame containing key scoring results
    helpfulnessScores: pd.DataFrame containing contributor specific scoring results
    auxiliaryNoteInfo: pd.DataFrame containing intermediate scoring state
    modelScoredNotes: pd.DataFrame containing scoredNotes result for a particular model
    modelHelpfulnessScores: None or pd.DataFrame containing helpfulnessScores result for a particular model
    modelauxiliaryNoteInfo: None or pd.DataFrame containing auxiliaryNoteInfo result for a particular model

  Returns:
    Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]
  """
  # Merge scoredNotes
  assert (set(modelScoredNotes.columns) & set(scoredNotes.columns)) == {
    c.noteIdKey
  }, "column names must be globally unique"
  scoredNotesSize = len(scoredNotes)
  scoredNotes = scoredNotes.merge(modelScoredNotes, on=c.noteIdKey, how="outer")
  assert len(scoredNotes) == scoredNotesSize, "scoredNotes should not expand"

  # Merge helpfulnessScores
  if modelHelpfulnessScores is not None:
    assert (set(modelHelpfulnessScores.columns) & set(helpfulnessScores.columns)) == {
      c.raterParticipantIdKey
    }, "column names must be globally unique"
    helpfulnessScores = helpfulnessScores.merge(
      modelHelpfulnessScores, on=c.raterParticipantIdKey, how="outer"
    )

  # Merge auxiliaryNoteInfo
  if modelauxiliaryNoteInfo is not None:
    assert (set(modelauxiliaryNoteInfo.columns) & set(auxiliaryNoteInfo.columns)) == {
      c.noteIdKey
    }, "column names must be globally unique"
    auxiliaryNoteInfoSize = len(auxiliaryNoteInfo)
    auxiliaryNoteInfo = auxiliaryNoteInfo.merge(modelauxiliaryNoteInfo, on=c.noteIdKey, how="outer")
    assert len(auxiliaryNoteInfo) == auxiliaryNoteInfoSize, "auxiliaryNoteInfo should not expand"

  return scoredNotes, helpfulnessScores, auxiliaryNoteInfo


def _run_scorer_parallelizable(
  scorer: Scorer,
  runParallel: bool,
  scoringArgs: ScoringArgs,
  dataLoader: Optional[CommunityNotesDataLoader] = None,
) -> Tuple[ModelResult, float]:
  if runParallel:
    assert dataLoader is not None, "must provide a dataLoader to run parallel"
    print(f"Since parallel, loading data in run_scoring process for {scorer.get_name()}")
    ## TODO: also load prescoringNoteModelOutput, raterParamsUnfiltered from data loader.
    _, ratings, noteStatusHistory, userEnrollment = dataLoader.get_data()

    scoringArgs.ratings = ratings
    scoringArgs.noteStatusHistory = noteStatusHistory
    scoringArgs.userEnrollment = userEnrollment
    if type(scoringArgs) == FinalScoringArgs:
      print(
        f"Loading prescoring model output for final scoring, in parallel for scorer {scorer.get_name()}."
      )
      prescoringNoteModelOutput, prescoringRaterParams = dataLoader.get_prescoring_model_output()
      scoringArgs.prescoringNoteModelOutput = prescoringNoteModelOutput
      scoringArgs.prescoringRaterModelOutput = prescoringRaterParams

  scorerStartTime = time.perf_counter()
  if type(scoringArgs) == PrescoringArgs:
    scoringResults = scorer.prescore(scoringArgs)
  elif type(scoringArgs) == FinalScoringArgs:
    scoringResults = scorer.score_final(scoringArgs)
  else:
    raise ValueError(f"Unknown scoringArgs type: {type(scoringArgs)}")
  scorerEndTime = time.perf_counter()

  return scoringResults, (scorerEndTime - scorerStartTime)


def _run_scorers(
  scorers: List[Scorer],
  scoringArgs: ScoringArgs,
  runParallel: bool = True,
  maxWorkers: Optional[int] = None,
  dataLoader: Optional[CommunityNotesDataLoader] = None,
) -> List[ModelResult]:
  """Applies all Community Notes models to user ratings and returns merged result.

  Each model must return a scoredNotes DF and may return helpfulnessScores and auxiliaryNoteInfo.
  scoredNotes and auxiliaryNoteInfo will be forced to contain one row per note to guarantee
  that all notes can be assigned a status during meta scoring regardless of whether any
  individual scoring algorithm scored the note.

  Args:
    scorers (List[Scorer]): Instantiated Scorer objects for note ranking.
    noteTopics: DF pairing notes with topics
    ratings (pd.DataFrame): Complete DF containing all ratings after preprocessing.
    noteStatusHistory (pd.DataFrame): one row per note; history of when note had each status
    userEnrollment (pd.DataFrame): The enrollment state for each contributor

  Returns:
    List[ModelResult]
  """
  # Apply scoring algorithms
  overallStartTime = time.perf_counter()
  if runParallel:
    with concurrent.futures.ProcessPoolExecutor(
      mp_context=multiprocessing.get_context("spawn"), max_workers=maxWorkers
    ) as executor:
      assert dataLoader is not None
      print(f"Starting parallel scorer execution with {len(scorers)} scorers.")
      # Pass mostly-empty scoringArgs: the data is too large to be copied in-memory to
      # each process, so must be re-loaded from disk by every scorer's dataLoader.
      scoringArgs.remove_large_args_for_multiprocessing()
      futures = [
        executor.submit(
          _run_scorer_parallelizable,
          scorer=scorer,
          runParallel=True,
          dataLoader=dataLoader,
          scoringArgs=scoringArgs,
        )
        for scorer in scorers
      ]
      modelResultsAndTimes = [f.result() for f in futures]
  else:
    modelResultsAndTimes = [
      _run_scorer_parallelizable(
        scorer=scorer,
        runParallel=False,
        scoringArgs=scoringArgs,
      )
      for scorer in scorers
    ]

  modelResultsTuple, scorerTimesTuple = zip(*modelResultsAndTimes)

  overallTime = time.perf_counter() - overallStartTime
  print(
    f"""----
    Completed individual scorers. Ran in parallel: {runParallel}.  Succeeded in {overallTime:.2f} seconds. 
    Individual scorers: (name, runtime): {list(zip(
      [scorer.get_name() for scorer in scorers],
      ['{:.2f}'.format(t/60.0) + " mins" for t in scorerTimesTuple]
    ))}
    ----"""
  )
  return list(modelResultsTuple)


def combine_prescorer_scorer_results(modelResults: List[ModelResult]):
  """
  Returns dfs with original columns plus an extra scorer name column.
  """
  assert isinstance(modelResults[0], ModelResult)

  prescoringNoteModelOutputList = []
  raterParamsUnfilteredMultiScorersList = []
  for modelResult in modelResults:
    if modelResult.scoredNotes is not None:
      modelResult.scoredNotes[c.scorerNameKey] = modelResult.scorerName
      prescoringNoteModelOutputList.append(modelResult.scoredNotes)
    if modelResult.helpfulnessScores is not None:
      modelResult.helpfulnessScores[c.scorerNameKey] = modelResult.scorerName
      raterParamsUnfilteredMultiScorersList.append(modelResult.helpfulnessScores)

  prescoringNoteModelOutput = pd.concat(prescoringNoteModelOutputList)
  raterParamsUnfilteredMultiScorers = pd.concat(raterParamsUnfilteredMultiScorersList)
  return prescoringNoteModelOutput, raterParamsUnfilteredMultiScorers


def combine_final_scorer_results(
  modelResultsFromEachScorer: List[ModelResult],
  noteStatusHistory: pd.DataFrame,
):
  """
  Returns:
    Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
      scoredNotes pd.DataFrame: one row per note contained note scores and parameters.
      helpfulnessScores pd.DataFrame: one row per user containing a column for each helpfulness score.
      auxiliaryNoteInfo pd.DataFrame: one row per note containing supplemental values used in scoring.
  """
  # Initialize return data frames.
  scoredNotes = noteStatusHistory[[c.noteIdKey]].drop_duplicates()
  auxiliaryNoteInfo = noteStatusHistory[[c.noteIdKey]].drop_duplicates()
  helpfulnessScores = pd.DataFrame({c.raterParticipantIdKey: []})

  # Merge the results
  for modelResult in modelResultsFromEachScorer:
    scoredNotes, helpfulnessScores, auxiliaryNoteInfo = _merge_results(
      scoredNotes,
      helpfulnessScores,
      auxiliaryNoteInfo,
      modelResult.scoredNotes,
      modelResult.helpfulnessScores,
      modelResult.auxiliaryNoteInfo,
    )
  scoredNotes, helpfulnessScores = coalesce_group_models(scoredNotes, helpfulnessScores)
  scoredNotes = coalesce_topic_models(scoredNotes)
  return scoredNotes, helpfulnessScores, auxiliaryNoteInfo


def meta_score(
  scorers: Dict[Scorers, List[Scorer]],
  scoredNotes: pd.DataFrame,
  auxiliaryNoteInfo: pd.DataFrame,
  lockedStatus: pd.DataFrame,
  enabledScorers: Optional[Set[Scorers]],
) -> Tuple[pd.DataFrame, pd.DataFrame]:
  """Determine final note status based on individual scoring results.

  This function applies logic merging the results of individual scorers to determine
  the final rating status of each note.  As part of determining the final rating status,
  we also apply scoring logic which exists independent of individual scorers (specifically
  note tag assignment and status locking).

  Args:
    scoredNotes: pd.DataFrame containing all scored note results.
    auxiliaryNoteInfo: pd.DataFrame containing tag aggregates
    lockedStatus: pd.DataFrame containing {noteId, status} pairs for all notes
    enabledScorers: if not None, set of which scorers should be instantiated and enabled

  Returns:
    Tuple[pd.DataFrame, pd.DataFrame]:
      scoredNotesCols pd.DataFrame: one row per note contained note scores and parameters.
      auxiliaryNoteInfoCols pd.DataFrame: one row per note containing adjusted and ratio tag values
  """
  # Temporarily merge helpfulness tag aggregates into scoredNotes so we can run InsufficientExplanation
  with c.time_block("Post-scorers: Meta Score: Setup"):
    assert len(scoredNotes) == len(auxiliaryNoteInfo)
    scoredNotes = scoredNotes.merge(
      auxiliaryNoteInfo[[c.noteIdKey] + c.helpfulTagsTSVOrder + c.notHelpfulTagsTSVOrder],
      on=c.noteIdKey,
    )
    assert len(scoredNotes) == len(auxiliaryNoteInfo)
    rules: List[scoring_rules.ScoringRule] = [
      scoring_rules.DefaultRule(RuleID.META_INITIAL_NMR, set(), c.needsMoreRatings)
    ]
    # Only attach meta-scoring rules for models which actually run.
    if enabledScorers is None or Scorers.MFExpansionPlusScorer in enabledScorers:
      # The MFExpansionPlusScorer should score a disjoint set of notes from MFExpansionScorer
      # and MFCoreScorer because it should score notes by EXPANSION_PLUS writers and should be
      # the only model to score notes by EXPANSION_PLUS writers.  This ordering is safe, where if
      # there is any bug and a note is scored by MFExpansionPlusScorer and another scorer, then
      # MFExpansionPlusScorer will have the lowest priority.
      rules.append(
        scoring_rules.ApplyModelResult(
          RuleID.EXPANSION_PLUS_MODEL, {RuleID.META_INITIAL_NMR}, c.expansionPlusRatingStatusKey
        )
      )
    if enabledScorers is None or Scorers.MFExpansionScorer in enabledScorers:
      rules.append(
        scoring_rules.ApplyModelResult(
          RuleID.EXPANSION_MODEL, {RuleID.META_INITIAL_NMR}, c.expansionRatingStatusKey
        )
      )
    if enabledScorers is None or Scorers.MFCoreScorer in enabledScorers:
      rules.append(
        scoring_rules.ApplyModelResult(
          RuleID.CORE_MODEL, {RuleID.META_INITIAL_NMR}, c.coreRatingStatusKey
        )
      )
    if enabledScorers is None or Scorers.MFGroupScorer in enabledScorers:
      # TODO: modify this code to work when MFExpansionScorer is disabled by the system test
      assert len(scorers[Scorers.MFCoreScorer]) == 1
      assert len(scorers[Scorers.MFExpansionScorer]) == 1
      coreScorer = scorers[Scorers.MFCoreScorer][0]
      assert isinstance(coreScorer, MFCoreScorer)
      expansionScorer = scorers[Scorers.MFExpansionScorer][0]
      assert isinstance(expansionScorer, MFExpansionScorer)
      coreCrhThreshold = coreScorer.get_crh_threshold()
      expansionCrhThreshold = expansionScorer.get_crh_threshold()
      for i in range(1, groupScorerCount + 1):
        if i != trialScoringGroup:
          rules.append(
            scoring_rules.ApplyGroupModelResult(
              RuleID[f"GROUP_MODEL_{i}"],
              {RuleID.EXPANSION_MODEL, RuleID.CORE_MODEL},
              i,
              coreCrhThreshold,
              expansionCrhThreshold,
            )
          )
        else:
          rules.append(
            scoring_rules.ApplyGroupModelResult(
              RuleID[f"GROUP_MODEL_{i}"],
              {RuleID.EXPANSION_MODEL, RuleID.CORE_MODEL},
              i,
              None,
              None,
              minSafeguardThreshold=None,
            )
          )
    if enabledScorers is None or Scorers.MFTopicScorer in enabledScorers:
      for topic in Topics:
        if topic == Topics.Unassigned:
          continue
        rules.append(
          scoring_rules.ApplyTopicModelResult(
            RuleID[f"TOPIC_MODEL_{topic.value}"],
            {RuleID.EXPANSION_PLUS_MODEL, RuleID.EXPANSION_MODEL, RuleID.CORE_MODEL},
            topic,
          )
        )
    rules.extend(
      [
        scoring_rules.ScoringDriftGuard(
          RuleID.SCORING_DRIFT_GUARD, {RuleID.CORE_MODEL}, lockedStatus
        ),
        # TODO: The rule below both sets tags for notes which are CRH / CRNH and unsets status for
        # any notes which are CRH / CRNH but don't have enough ratings to assign two tags.  The later
        # behavior can lead to unsetting locked status.  We should refactor this code to (1) remove
        # the behavior which unsets status (instead tags will be assigned on a best effort basis) and
        # (2) set tags in logic which is not run as a ScoringRule (since ScoringRules function to
        # update note status).
        scoring_rules.InsufficientExplanation(
          RuleID.INSUFFICIENT_EXPLANATION,
          {RuleID.CORE_MODEL},
          c.needsMoreRatings,
          c.minRatingsToGetTag,
          c.minTagsNeededForStatus,
        ),
      ]
    )
    scoredNotes[c.firstTagKey] = np.nan
    scoredNotes[c.secondTagKey] = np.nan

  with c.time_block("Post-scorers: Meta Score: Apply Scoring Rules"):
    scoringResult = scoring_rules.apply_scoring_rules(
      scoredNotes,
      rules,
      c.finalRatingStatusKey,
      c.metaScorerActiveRulesKey,
      decidedByColumn=c.decidedByKey,
    )

  with c.time_block("Post-scorers: Meta Score: Preparing Return Values"):
    scoredNotesCols = scoringResult[
      [
        c.noteIdKey,
        c.finalRatingStatusKey,
        c.metaScorerActiveRulesKey,
        c.firstTagKey,
        c.secondTagKey,
        c.decidedByKey,
      ]
    ]
    auxiliaryNoteInfoCols = scoringResult[
      [
        c.noteIdKey,
        c.currentlyRatedHelpfulBoolKey,
        c.currentlyRatedNotHelpfulBoolKey,
        c.awaitingMoreRatingsBoolKey,
        c.unlockedRatingStatusKey,
      ]
    ]
  return scoredNotesCols, auxiliaryNoteInfoCols


def _compute_note_stats(
  ratings: pd.DataFrame, noteStatusHistory: pd.DataFrame
) -> Tuple[pd.DataFrame, pd.DataFrame]:
  """Generates DFs containing aggregate / global properties for each note.

  This function computes note aggregates over ratings and merges in selected fields
  from noteStatusHistory.  This function runs independent of individual scorers and
  augments the results of individual scorers because individual scorer may elect to
  consider only a subset of notes or ratings.  Computing on all available data after
  scorers have run guarantees completeness over all Community Notes data.

  Args:
    ratings: pd.DataFrame continaing *all* ratings on *all* notes from *all* users.
    noteStatusHistory: pd.DataFrame containing complete noteStatusHistory for all notes.

  Returns:
    Tuple[pd.DataFrame, pd.DataFrame]:
      scoredNotesCols pd.DataFrame: one row per note contained note scores and parameters.
      auxiliaryNoteInfoCols pd.DataFrame: one row per note containing adjusted and ratio tag values
  """
  noteStats = note_ratings.compute_note_stats(ratings, noteStatusHistory)
  scoredNotesCols = noteStats[
    [c.noteIdKey, c.classificationKey, c.createdAtMillisKey, c.numRatingsKey]
  ]
  auxiliaryNoteInfoCols = noteStats[
    [
      c.noteIdKey,
      c.noteAuthorParticipantIdKey,
      c.createdAtMillisKey,
      c.numRatingsLast28DaysKey,
      c.currentLabelKey,
    ]
    + (c.helpfulTagsTSVOrder + c.notHelpfulTagsTSVOrder)
  ]
  return scoredNotesCols, auxiliaryNoteInfoCols


def _compute_helpfulness_scores(
  ratings: pd.DataFrame,
  scoredNotes: pd.DataFrame,
  auxiliaryNoteInfo: pd.DataFrame,
  helpfulnessScores: pd.DataFrame,
  noteStatusHistory: pd.DataFrame,
  userEnrollment: pd.DataFrame,
) -> pd.DataFrame:
  """Computes usage statistics for Community Notes contributors.

  This function takes as input scoredNotes and auxiliaryNoteInfo (which represent the scoring
  results and associated statistics), helpfulnessScores (which contains any contributor metrics
  exported as a side effect of note scoring), ratings (which contains raw data about direct user
  contributions) along with noteStatusHistory (used to determine when notes were first assigned
  status) and userEnrollment (used to determine writing ability for users).

  See documentation below for additional information about contributor scores:
  https://twitter.github.io/communitynotes/contributor-scores/.

  Args:
      ratings (pd.DataFrame): preprocessed ratings
      scoredNotes (pd.DataFrame): notes with scores returned by MF scoring algorithm
      auxiliaryNoteInfo (pd.DataFrame): additional fields generated during note scoring
      helpfulnessScores (pd.DataFrame): BasicReputation scores for all raters
      noteStatusHistory (pd.DataFrame): one row per note; history of when note had each status
      userEnrollment (pd.DataFrame): The enrollment state for each contributor

  Returns:
      helpfulnessScores pd.DataFrame: one row per user containing a column for each helpfulness score.
  """
  with c.time_block("Meta Helpfulness Scorers: Setup"):
    # Generate a unified view of note scoring information for computing contributor stats
    assert len(scoredNotes) == len(auxiliaryNoteInfo), "notes in both note inputs must match"
    scoredNotesWithStats = scoredNotes.merge(
      # noteId and timestamp are the only common fields, and should always be equal.
      auxiliaryNoteInfo,
      on=[c.noteIdKey, c.createdAtMillisKey],
      how="inner",
    )[
      [
        c.noteIdKey,
        c.finalRatingStatusKey,
        c.coreNoteInterceptKey,
        c.currentlyRatedHelpfulBoolKey,
        c.currentlyRatedNotHelpfulBoolKey,
        c.awaitingMoreRatingsBoolKey,
        c.createdAtMillisKey,
        c.noteAuthorParticipantIdKey,
        c.numRatingsKey,
        c.numRatingsLast28DaysKey,
      ]
    ]
    assert len(scoredNotesWithStats) == len(scoredNotes)

  with c.time_block("Meta Helpfulness Scores: Contributor Scores"):
    # Return one row per rater with stats including trackrecord identifying note labels.
    contributorScores = contributor_state.get_contributor_scores(
      scoredNotesWithStats,
      ratings,
      noteStatusHistory,
    )
  with c.time_block("Meta Helpfulness Scorers: Contributor State"):
    contributorState, prevState = contributor_state.get_contributor_state(
      scoredNotesWithStats,
      ratings,
      noteStatusHistory,
      userEnrollment,
    )

  with c.time_block("Meta Helpfulness Scorers: Combining"):
    # We need to do an outer merge because the contributor can have a state (be a new user)
    # without any notes or ratings.
    contributorScores = contributorScores.merge(
      contributorState[
        [
          c.raterParticipantIdKey,
          c.timestampOfLastStateChange,
          c.enrollmentState,
          c.successfulRatingNeededToEarnIn,
          c.authorTopNotHelpfulTagValues,
          c.isEmergingWriterKey,
          c.numberOfTimesEarnedOutKey,
          c.ratingImpact,
          c.hasCrnhSinceEarnOut,
        ]
      ],
      on=c.raterParticipantIdKey,
      how="outer",
    )
    contributorScores = contributor_state.single_trigger_earn_out(contributorScores)
    contributorScores = contributor_state.calculate_ri_to_earn_in(contributorScores)

    # Consolidates all information on raters / authors.
    helpfulnessScores = helpfulnessScores.merge(
      contributorScores,
      on=c.raterParticipantIdKey,
      how="outer",
    )
    # Pass timestampOfLastEarnOut through to raterModelOutput.
    helpfulnessScores = helpfulnessScores.merge(
      prevState,
      left_on=c.raterParticipantIdKey,
      right_on=c.participantIdKey,
      how="left",
    ).drop(c.participantIdKey, axis=1)

    # For users who did not earn a new enrollmentState, carry over the previous one
    helpfulnessScores[c.enrollmentState] = helpfulnessScores[c.enrollmentState].fillna(
      helpfulnessScores[c.enrollmentState + "_prev"]
    )
    helpfulnessScores.drop(columns=[c.enrollmentState + "_prev"], inplace=True)

    # If field is not set by userEvent or by update script, ok to default to 1
    helpfulnessScores[c.timestampOfLastEarnOut].fillna(1, inplace=True)

  return helpfulnessScores


def _add_deprecated_columns(scoredNotes: pd.DataFrame) -> pd.DataFrame:
  """Impute columns which are no longer used but must be maintained in output.

  Args:
    scoredNotes: DataFrame containing note scoring output

  Returns:
    scoredNotes augmented to include deprecated columns filled with dummy values
  """
  for column, columnType in c.deprecatedNoteModelOutputTSVColumnsAndTypes:
    assert column not in scoredNotes.columns
    if columnType == np.double:
      scoredNotes[column] = np.nan
    elif columnType == str:
      scoredNotes[column] = ""
    else:
      assert False, f"column type {columnType} unsupported"
  return scoredNotes


def _validate(
  scoredNotes: pd.DataFrame,
  helpfulnessScores: pd.DataFrame,
  noteStatusHistory: pd.DataFrame,
  auxiliaryNoteInfo: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
  """Guarantee that each dataframe has the expected columns in the correct order.

  Args:
    scoredNotes (pd.DataFrame): notes with scores returned by MF scoring algorithm
    helpfulnessScores (pd.DataFrame): BasicReputation scores for all raters
    noteStatusHistory (pd.DataFrame): one row per note; history of when note had each status
    auxiliaryNoteInfo (pd.DataFrame): additional fields generated during note scoring

  Returns:
    Input arguments with columns potentially re-ordered.
  """
  assert set(scoredNotes.columns) == set(
    c.noteModelOutputTSVColumns
  ), f"Got {sorted(scoredNotes.columns)}, expected {sorted(c.noteModelOutputTSVColumns)}"
  scoredNotes = scoredNotes[c.noteModelOutputTSVColumns]
  assert set(helpfulnessScores.columns) == set(
    c.raterModelOutputTSVColumns
  ), f"Got {sorted(helpfulnessScores.columns)}, expected {sorted(c.raterModelOutputTSVColumns)}"
  helpfulnessScores = helpfulnessScores[c.raterModelOutputTSVColumns]
  assert set(noteStatusHistory.columns) == set(
    c.noteStatusHistoryTSVColumns
  ), f"Got {sorted(noteStatusHistory.columns)}, expected {sorted(c.noteStatusHistoryTSVColumns)}"
  noteStatusHistory = noteStatusHistory[c.noteStatusHistoryTSVColumns]
  assert set(auxiliaryNoteInfo.columns) == set(
    c.auxiliaryScoredNotesTSVColumns
  ), f"Got {sorted(auxiliaryNoteInfo.columns)}, expected {sorted(c.auxiliaryScoredNotesTSVColumns)}"
  auxiliaryNoteInfo = auxiliaryNoteInfo[c.auxiliaryScoredNotesTSVColumns]
  return (scoredNotes, helpfulnessScores, noteStatusHistory, auxiliaryNoteInfo)


def run_prescoring(
  notes: pd.DataFrame,
  ratings: pd.DataFrame,
  noteStatusHistory: pd.DataFrame,
  userEnrollment: pd.DataFrame,
  seed: Optional[int] = None,
  enabledScorers: Optional[Set[Scorers]] = None,
  runParallel: bool = True,
  dataLoader: Optional[CommunityNotesDataLoader] = None,
  useStableInitialization: bool = True,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
  with c.time_block("Note Topic Assignment"):
    topicModel = TopicModel()
    noteTopics = topicModel.get_note_topics(notes)

  scorers = _get_scorers(
    seed=seed,
    pseudoraters=False,
    enabledScorers=enabledScorers,
    useStableInitialization=useStableInitialization,
  )

  prescoringModelResultsFromAllScorers = _run_scorers(
    scorers=list(chain(*scorers.values())),
    scoringArgs=PrescoringArgs(
      noteTopics=noteTopics,
      ratings=ratings,
      noteStatusHistory=noteStatusHistory,
      userEnrollment=userEnrollment,
    ),
    runParallel=runParallel,
    dataLoader=dataLoader,
    # Restrict parallelism to 6 processes.  Memory usage scales linearly with the number of
    # processes and 6 is enough that the limiting factor continues to be the longest running
    # scorer (i.e. we would not finish faster with >6 worker processes.)
    maxWorkers=6,
  )

  (
    prescoringNoteModelOutput,
    prescoringRaterModelOutput,
  ) = combine_prescorer_scorer_results(prescoringModelResultsFromAllScorers)

  return prescoringNoteModelOutput, prescoringRaterModelOutput


def run_final_scoring(
  notes: pd.DataFrame,
  ratings: pd.DataFrame,
  noteStatusHistory: pd.DataFrame,
  userEnrollment: pd.DataFrame,
  seed: Optional[int] = None,
  pseudoraters: Optional[bool] = True,
  enabledScorers: Optional[Set[Scorers]] = None,
  strictColumns: bool = True,
  runParallel: bool = True,
  dataLoader: Optional[CommunityNotesDataLoader] = None,
  useStableInitialization: bool = True,
  prescoringNoteModelOutput: Optional[pd.DataFrame] = None,
  prescoringRaterModelOutput: Optional[pd.DataFrame] = None,
):
  with c.time_block("Note Topic Assignment"):
    topicModel = TopicModel()
    noteTopics = topicModel.get_note_topics(notes)

  scorers = _get_scorers(
    seed, pseudoraters, enabledScorers, useStableInitialization=useStableInitialization
  )

  modelResults = _run_scorers(
    scorers=list(chain(*scorers.values())),
    scoringArgs=FinalScoringArgs(
      noteTopics,
      ratings,
      noteStatusHistory,
      userEnrollment,
      prescoringNoteModelOutput=prescoringNoteModelOutput,
      prescoringRaterModelOutput=prescoringRaterModelOutput,
    ),
    runParallel=runParallel,
    dataLoader=dataLoader,
    # Restrict parallelism to 6 processes.  Memory usage scales linearly with the number of
    # processes and 6 is enough that the limiting factor continues to be the longest running
    # scorer (i.e. we would not finish faster with >6 worker processes.)
    maxWorkers=6,
  )

  scoredNotes, helpfulnessScores, auxiliaryNoteInfo = combine_final_scorer_results(
    modelResults, noteStatusHistory
  )

  return post_scoring(
    scorers,
    scoredNotes,
    helpfulnessScores,
    auxiliaryNoteInfo,
    ratings,
    noteStatusHistory,
    userEnrollment,
    enabledScorers,
    strictColumns,
  )


def post_scoring(
  scorers: Dict[Scorers, List[Scorer]],
  scoredNotes: pd.DataFrame,
  helpfulnessScores: pd.DataFrame,
  auxiliaryNoteInfo: pd.DataFrame,
  ratings: pd.DataFrame,
  noteStatusHistory: pd.DataFrame,
  userEnrollment: pd.DataFrame,
  enabledScorers: Optional[Set[Scorers]] = None,
  strictColumns: bool = True,
):
  """
  Apply individual scoring models and obtained merged result.
  """
  postScoringStartTime = time.time()
  # Augment scoredNotes and auxiliaryNoteInfo with additional attributes for each note
  # which are computed over the corpus of notes / ratings as a whole and are independent
  # of any particular model.

  with c.time_block("Post-scorers: Compute note stats"):
    scoredNotesCols, auxiliaryNoteInfoCols = _compute_note_stats(ratings, noteStatusHistory)
    scoredNotes = scoredNotes.merge(scoredNotesCols, on=c.noteIdKey)
    auxiliaryNoteInfo = auxiliaryNoteInfo.merge(auxiliaryNoteInfoCols, on=c.noteIdKey)

  # Assign final status to notes based on individual model scores and note attributes.
  with c.time_block("Post-scorers: Meta score"):
    scoredNotesCols, auxiliaryNoteInfoCols = meta_score(
      scorers,
      scoredNotes,
      auxiliaryNoteInfo,
      noteStatusHistory[[c.noteIdKey, c.lockedStatusKey]],
      enabledScorers,
    )

  with c.time_block("Post-scorers: Join scored notes"):
    scoredNotes = scoredNotes.merge(scoredNotesCols, on=c.noteIdKey)
    scoredNotes[c.timestampMillisOfNoteCurrentLabelKey] = c.epochMillis
    auxiliaryNoteInfo = auxiliaryNoteInfo.merge(auxiliaryNoteInfoCols, on=c.noteIdKey)

    # Validate that no notes were dropped or duplicated.
    assert len(scoredNotes) == len(
      noteStatusHistory
    ), "noteStatusHistory should be complete, and all notes should be scored."
    assert len(auxiliaryNoteInfo) == len(
      noteStatusHistory
    ), "noteStatusHistory should be complete, and all notes should be scored."

  # Compute contribution statistics and enrollment state for users.
  with c.time_block("Post-scorers: Compute helpfulness scores"):
    helpfulnessScores = _compute_helpfulness_scores(
      ratings, scoredNotes, auxiliaryNoteInfo, helpfulnessScores, noteStatusHistory, userEnrollment
    )

  # Merge scoring results into noteStatusHistory.
  with c.time_block("Post-scorers: Update note status history"):
    newNoteStatusHistory = note_status_history.update_note_status_history(
      noteStatusHistory, scoredNotes
    )
    assert len(newNoteStatusHistory) == len(
      noteStatusHistory
    ), "noteStatusHistory should contain all notes after preprocessing"

  # Skip validation and selection out output columns if the set of scorers is overridden.
  with c.time_block("Post-scorers: finalize output columns"):
    scoredNotes = _add_deprecated_columns(scoredNotes)
    if strictColumns:
      scoredNotes, helpfulnessScores, newNoteStatusHistory, auxiliaryNoteInfo = _validate(
        scoredNotes, helpfulnessScores, newNoteStatusHistory, auxiliaryNoteInfo
      )

  print(f"Meta scoring elapsed time: {((time.time() - postScoringStartTime)/60.0):.2f} minutes.")
  return scoredNotes, helpfulnessScores, newNoteStatusHistory, auxiliaryNoteInfo


def run_scoring(
  notes: pd.DataFrame,
  ratings: pd.DataFrame,
  noteStatusHistory: pd.DataFrame,
  userEnrollment: pd.DataFrame,
  seed: Optional[int] = None,
  pseudoraters: Optional[bool] = True,
  enabledScorers: Optional[Set[Scorers]] = None,
  strictColumns: bool = True,
  runParallel: bool = True,
  dataLoader: Optional[CommunityNotesDataLoader] = None,
  useStableInitialization: bool = True,
  writePrescoringScoringOutputCallback: Optional[
    Callable[[pd.DataFrame, pd.DataFrame], None]
  ] = None,
  filterPrescoringInputToSimulateDelayInHours: Optional[int] = None,
):
  """Runs both phases of scoring consecutively. Only for adhoc/testing use.
  In prod, we run each phase as a separate binary.

  Wrapper around run_prescoring and run_final_scoring.

  Invokes note scoring algorithms, merges results and computes user stats.

  Args:
    ratings (pd.DataFrame): preprocessed ratings
    noteStatusHistory (pd.DataFrame): one row per note; history of when note had each status
    userEnrollment (pd.DataFrame): The enrollment state for each contributor
    seed (int, optional): if not None, base distinct seeds for the first and second MF rounds on this value
    pseudoraters (bool, optional): if True, compute optional pseudorater confidence intervals
    enabledScorers (Set[Scorers], optional): Scorers which should be instantiated
    strictColumns (bool, optional): if True, validate which columns are present
    runParallel (bool, optional): if True, run algorithms in parallel
    dataLoader (CommunityNotesDataLoader, optional): dataLoader provided to parallel execution
    useStableInitialization
    writePrescoringScoringOutputCallback
    filterPrescoringInputToSimulateDelayInHours

  Returns:
    Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
      scoredNotes pd.DataFrame: one row per note contained note scores and parameters.
      helpfulnessScores pd.DataFrame: one row per user containing a column for each helpfulness score.
      noteStatusHistory pd.DataFrame: one row per note containing when they got their most recent statuses.
      auxiliaryNoteInfo: one row per note containing adjusted and ratio tag values
  """

  # Filter input data for prescoring to simulate running prescoring earlier than final scoring
  if filterPrescoringInputToSimulateDelayInHours is not None:
    latestRatingMillis = ratings[c.createdAtMillisKey].max()
    cutoffMillis = latestRatingMillis - (
      filterPrescoringInputToSimulateDelayInHours * 60 * 60 * 1000
    )
    print(
      f"""
      Filtering input data for prescoring to simulate running prescoring earlier than final scoring.
      Latest rating timestamp: {pd.to_datetime(latestRatingMillis, unit='ms')}
      Cutoff timestamp: {pd.to_datetime(cutoffMillis, unit='ms')} ({filterPrescoringInputToSimulateDelayInHours} hours before)
    """
    )
    prescoringNotesInput = notes[notes[c.createdAtMillisKey] < cutoffMillis].copy()
    prescoringRatingsInput = ratings[ratings[c.createdAtMillisKey] < cutoffMillis].copy()
  else:
    prescoringNotesInput = notes
    prescoringRatingsInput = ratings

  (
    prescoringNoteModelOutput,
    prescoringRaterModelOutput,
  ) = run_prescoring(
    notes=prescoringNotesInput,
    ratings=prescoringRatingsInput,
    noteStatusHistory=noteStatusHistory,
    userEnrollment=userEnrollment,
    seed=seed,
    enabledScorers=enabledScorers,
    runParallel=runParallel,
    dataLoader=dataLoader,
    useStableInitialization=useStableInitialization,
  )

  print("We invoked run_scoring and are now in between prescoring and scoring.")
  if writePrescoringScoringOutputCallback is not None:
    with c.time_block("Writing prescoring output."):
      writePrescoringScoringOutputCallback(prescoringNoteModelOutput, prescoringRaterModelOutput)
  print("Starting final scoring")

  return run_final_scoring(
    notes=notes,
    ratings=ratings,
    noteStatusHistory=noteStatusHistory,
    userEnrollment=userEnrollment,
    seed=seed,
    pseudoraters=pseudoraters,
    enabledScorers=enabledScorers,
    strictColumns=strictColumns,
    runParallel=runParallel,
    dataLoader=dataLoader,
    useStableInitialization=useStableInitialization,
    prescoringNoteModelOutput=prescoringNoteModelOutput,
    prescoringRaterModelOutput=prescoringRaterModelOutput,
  )
