import { ConsumerProfile } from './letterTemplate';

export interface DraftLetterRequest {
  consumer: ConsumerProfile;
}

export interface AuthorizeLetterRequest {
  /** Required, literal true. A second, explicit guard beyond the confirm
   * step -- defense in depth against any accidental/automated call reaching
   * this endpoint, since it is the only one that spends real money. */
  explicitApproval: true;
}
